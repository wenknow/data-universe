import contextlib
import datetime as dt
import stat
import bittensor as bt
import sqlite3
import threading
from typing import Any, Dict, List, Optional, Set, Tuple
from common.data import CompressedMinerIndex, DataLabel
from common.data_v2 import (
    ScorableDataEntityBucket,
    ScorableMinerIndex,
    DataBoxMiner,
    DataBoxLabelSize,
    DataBoxAgeSize,
)
from storage.validator.validator_storage import (
    ValidatorStorage,
)

from datadog import statsd


class AutoIncrementDict:
    """A dictionary that automatically assigns ids to keys.

    Provides O(1) ability to insert a key and get its id, and to lookup the key for an id.

    Not thread safe.
    """

    def __init__(self):
        self.available_ids = set()
        self.items = []
        self.indexes = {}

    def get_or_insert(self, key: Any) -> int:
        if key not in self.indexes:
            if self.available_ids:
                key_id = self.available_ids.pop()
                self.items[key_id] = key
                self.indexes[key] = key_id
            else:
                self.items.append(key)
                self.indexes[key] = len(self.items) - 1

        return self.indexes[key]

    def get_by_id(self, id: int) -> Any:
        return self.items[id]

    def delete_key(self, key: Any):
        if key in self.indexes:
            key_id = self.indexes[key]
            self.items[key_id] = None
            del self.indexes[key]
            self.available_ids.add(key_id)


# Use a timezone aware adapter for timestamp columns.
def tz_aware_timestamp_adapter(val):
    datepart, timepart = val.split(b" ")
    year, month, day = map(int, datepart.split(b"-"))

    if b"+" in timepart:
        timepart, tz_offset = timepart.rsplit(b"+", 1)
        if tz_offset == b"00:00":
            tzinfo = dt.timezone.utc
        else:
            hours, minutes = map(int, tz_offset.split(b":", 1))
            tzinfo = dt.timezone(dt.timedelta(hours=hours, minutes=minutes))
    elif b"-" in timepart:
        timepart, tz_offset = timepart.rsplit(b"-", 1)
        if tz_offset == b"00:00":
            tzinfo = dt.timezone.utc
        else:
            hours, minutes = map(int, tz_offset.split(b":", 1))
            tzinfo = dt.timezone(dt.timedelta(hours=-hours, minutes=-minutes))
    else:
        tzinfo = None

    timepart_full = timepart.split(b".")
    hours, minutes, seconds = map(int, timepart_full[0].split(b":"))

    if len(timepart_full) == 2:
        microseconds = int("{:0<6.6}".format(timepart_full[1].decode()))
    else:
        microseconds = 0

    val = dt.datetime(year, month, day, hours, minutes, seconds, microseconds, tzinfo)

    return val


class SqliteMemoryValidatorStorage(ValidatorStorage):
    """Sqlite in-memory backed Validator Storage"""

    # Integer Primary Key = ROWID alias which is auto-increment when assigning NULL on insert.
    MINER_TABLE_CREATE = """CREATE TABLE IF NOT EXISTS Miner (
                            minerId     INTEGER         PRIMARY KEY,
                            hotkey      VARCHAR(64)     NOT NULL,
                            lastUpdated TIMESTAMP(6)    NOT NULL,
                            credibility FLOAT           NOT NULL    DEFAULT 0.00,
                            UNIQUE(hotkey)
                            )"""

    MINER_TABLE_CREDIBILTY_INDEX = """CREATE INDEX IF NOT EXISTS miner_credibility_index
                                      ON Miner (minerId, credibility)"""

    # Updated Primary table in which the DataEntityBuckets for all miners are stored.
    REDDIT_TABLE_CREATE = """CREATE TABLE IF NOT EXISTS Reddit (
                                    minerId             INTEGER         NOT NULL,
                                    labelId             INTEGER         NOT NULL,
                                    timeBucketId        INTEGER         NOT NULL,
                                    contentSizeBytes    INTEGER         NOT NULL,
                                    PRIMARY KEY(minerId, labelId, timeBucketId)
                                    ) WITHOUT ROWID"""

    TWITTER_TABLE_CREATE = """CREATE TABLE IF NOT EXISTS Twitter (
                                    minerId             INTEGER         NOT NULL,
                                    labelId             INTEGER         NOT NULL,
                                    timeBucketId        INTEGER         NOT NULL,
                                    contentSizeBytes    INTEGER         NOT NULL,
                                    PRIMARY KEY(minerId, labelId, timeBucketId)
                                    ) WITHOUT ROWID"""

    REDDIT_INDEX_TABLE_BUCKET_SIZE_INDEX = """CREATE INDEX IF NOT EXISTS bucket_size_index
                                             ON Reddit (labelId, timeBucketId, contentSizeBytes)"""

    TWITTER_INDEX_TABLE_BUCKET_SIZE_INDEX = """CREATE INDEX IF NOT EXISTS bucket_size_index
                                             ON Twitter (labelId, timeBucketId, contentSizeBytes)"""

    def __init__(self):
        sqlite3.register_converter("timestamp", tz_aware_timestamp_adapter)

        self.continuous_connection_do_not_reuse = self._create_connection()
        self.label_dict = AutoIncrementDict()

        with contextlib.closing(self._create_connection()) as connection:
            cursor = connection.cursor()

            # Create the Miner table (if it does not already exist).
            cursor.execute(SqliteMemoryValidatorStorage.MINER_TABLE_CREATE)
            cursor.execute(SqliteMemoryValidatorStorage.MINER_TABLE_CREDIBILTY_INDEX)

            # Create the Index table (if it does not already exist).
            cursor.execute(SqliteMemoryValidatorStorage.REDDIT_TABLE_CREATE)
            cursor.execute(
                SqliteMemoryValidatorStorage.REDDIT_INDEX_TABLE_BUCKET_SIZE_INDEX
            )
            cursor.execute(SqliteMemoryValidatorStorage.TWITTER_TABLE_CREATE)
            cursor.execute(
                SqliteMemoryValidatorStorage.TWITTER_INDEX_TABLE_BUCKET_SIZE_INDEX
            )

            # Lock to avoid concurrency issues on interacting with the database.
            self.lock = threading.RLock()

    def _create_connection(self):
        # Create the database if it doesn't exist, defaulting to the local directory.
        # Use PARSE_DECLTYPES to convert accessed values into the appropriate type.
        connection = sqlite3.connect(
            "file::memory:?cache=shared",
            uri=True,
            detect_types=sqlite3.PARSE_DECLTYPES,
            timeout=120.0,
        )
        # Avoid using a row_factory that would allow parsing results by column name for performance.
        # connection.row_factory = sqlite3.Row
        connection.isolation_level = None
        return connection

    def _upsert_miner(self, hotkey: str, now_str: str, credibility: float) -> int:
        miner_id = 0

        with self.lock:
            with contextlib.closing(self._create_connection()) as connection:
                cursor = connection.cursor()

                cursor.execute(
                    "UPDATE OR IGNORE Miner SET lastUpdated=?, credibility=? WHERE hotkey=?",
                    [now_str, credibility, hotkey],
                )
                cursor.execute(
                    """INSERT OR IGNORE INTO Miner (hotkey, lastUpdated, credibility) VALUES (?, ?, ?)""",
                    [hotkey, now_str, credibility],
                )
                connection.commit()

                # Then we get the existing or newly created minerId
                cursor.execute("SELECT minerId FROM Miner WHERE hotkey = ?", [hotkey])
                miner_id = cursor.fetchone()[0]

        return miner_id

    def _label_value_parse(self, label: Optional[DataLabel]) -> str:
        """Parses the value to store in the database out of an Optional DataLabel."""
        return "NULL" if (label is None) else label.value

    def _label_value_parse_str(self, label: Optional[str]) -> str:
        """Same as _label_value_parse but with a string as input"""
        return "NULL" if (label is None) else label.casefold()

    @statsd.timed("storage.validator.upsert_compressed_miner_index")
    def upsert_compressed_miner_index(
        self, index: CompressedMinerIndex, hotkey: str, credibility: float
    ):
        """Stores the index for all of the data that a specific miner promises to provide."""

        bt.logging.trace(
            f"{hotkey}: Upserting miner index with {CompressedMinerIndex.bucket_count(index)} buckets"
        )

        now_str = dt.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S.%f")

        # Upsert this Validator's minerId for the specified hotkey.
        miner_id = self._upsert_miner(hotkey, now_str, credibility)

        # Parse every DataEntityBucket from the index into a list of values to insert.
        reddit_values = []
        twitter_values = []
        for source, compressed_buckets in index.sources.items():
            values = reddit_values if source == 1 else twitter_values
            for compressed_bucket in compressed_buckets:
                for time_bucket_id, size_bytes in zip(
                    compressed_bucket.time_bucket_ids, compressed_bucket.sizes_bytes
                ):
                    try:
                        values.append(
                            [
                                miner_id,
                                self.label_dict.get_or_insert(
                                    self._label_value_parse_str(compressed_bucket.label)
                                ),
                                time_bucket_id,
                                size_bytes,
                            ]
                        )
                    except:
                        # In the case that we fail to get a label (due to unsupported characters) we drop just that one bucket.
                        pass

        with self.lock:
            # Clear the previous keys for this miner.
            self._delete_miner_index(hotkey)

            with contextlib.closing(self._create_connection()) as connection:
                cursor = connection.cursor()
                # Insert the new keys. (Ignore into to defend against a miner giving us multiple duplicate rows.)
                # Batch in groups of 1m if necessary to avoid congestion issues.
                cursor.executemany(
                    """INSERT OR IGNORE INTO Reddit (minerId, labelId, timeBucketId, contentSizeBytes) VALUES (?, ?, ?, ?)""",
                    reddit_values,
                )
                cursor.executemany(
                    """INSERT OR IGNORE INTO Twitter (minerId, labelId, timeBucketId, contentSizeBytes) VALUES (?, ?, ?, ?)""",
                    twitter_values,
                )
                connection.commit()

    def _process_read_results(self, cursor, source, scored_buckets):
        # For each row (representing a DataEntityBucket and Uniqueness) turn it into a ScorableDataEntityBucket.
        for row in cursor:
            label_value = self.label_dict.get_by_id(row[0])

            # Add the bucket to the list of scored buckets on the overall index.
            scored_buckets.append(
                ScorableDataEntityBucket(
                    time_bucket_id=int(row[1]),
                    source=source,
                    label=label_value if label_value != "NULL" else None,
                    size_bytes=int(row[2] if row[2] else 0),
                    scorable_bytes=int(row[3] if row[3] else 0),
                )
            )

    @statsd.timed("storage.validator.read_miner_index")
    def read_miner_index(
        self,
        miner_hotkey: str,
    ) -> Optional[ScorableMinerIndex]:
        """Gets a scored index for all of the data that a specific miner promises to provide."""

        with self.lock:
            with contextlib.closing(self._create_connection()) as connection:
                cursor = connection.cursor()
                cursor.execute(
                    "SELECT minerId, lastUpdated, credibility from Miner WHERE hotkey = ?",
                    [miner_hotkey],
                )
                result = cursor.fetchone()
                if result is None:
                    return None

                miner_id = result[0]
                last_updated = result[1]
                miner_credibility = result[2]

                # Create to a list to hold each of the ScorableDataEntityBuckets we generate for this miner.
                scored_data_entity_buckets = []
                for source in [1, 2]:
                    table = "Reddit" if source == 1 else "Twitter"

                    # Get all the DataEntityBuckets for this miner joined to the total content size of like buckets.
                    sql_string = f"""WITH
                                    TempBuckets AS (
                                        SELECT labelId, timeBucketId
                                        FROM {table}
                                        WHERE MinerId = ?
                                    ),
                                    TempAgg AS (
                                        SELECT labelId, timeBucketId,
                                        SUM(contentSizeBytes * credibility) as totalAdjContentSizeBytes
                                        FROM {table}
                                        INNER JOIN TempBuckets USING (labelId, timeBucketId)
                                        JOIN Miner USING (minerId)
                                        GROUP BY labelId, timeBucketId
                                    )
                                    SELECT labelId, timeBucketId, contentSizeBytes,
                                        (contentSizeBytes * (contentSizeBytes * ?) / TempAgg.totalAdjContentSizeBytes) as scorableBytes
                                    FROM {table}
                                    LEFT JOIN TempAgg USING (labelId, timeBucketId)
                                    WHERE minerId = ?"""

                    cursor.execute(sql_string, [miner_id, miner_credibility, miner_id])
                    self._process_read_results(
                        cursor, source, scored_data_entity_buckets
                    )

                scored_index = ScorableMinerIndex(
                    scorable_data_entity_buckets=scored_data_entity_buckets,
                    last_updated=last_updated,
                )

                return scored_index

    @statsd.timed("storage.validator._delete_miner_index")
    def _delete_miner_index(self, miner_hotkey: str):
        """Removes the index for the specified miner."""

        bt.logging.trace(f"{miner_hotkey}: Deleting miner index")

        with contextlib.closing(self._create_connection()) as connection:
            cursor = connection.cursor()

            cursor.execute("SELECT minerId FROM Miner WHERE hotkey = ?", [miner_hotkey])

            # Delete the rows for the specified miner.
            result = cursor.fetchone()
            if result is not None:
                cursor.execute("DELETE FROM Reddit WHERE minerId = ?", [result[0]])
                cursor.execute("DELETE FROM Twitter WHERE minerId = ?", [result[0]])
                connection.commit()

    def delete_miner(self, hotkey: str):
        """Removes the index and miner details for the specified miner."""
        with self.lock:
            self._delete_miner_index(hotkey)

            with contextlib.closing(self._create_connection()) as connection:
                cursor = connection.cursor()
                cursor.execute("DELETE FROM Miner WHERE hotkey = ?", [hotkey])

    def read_miner_last_updated(self, miner_hotkey: str) -> Optional[dt.datetime]:
        """Gets when a specific miner was last updated."""
        with self.lock:
            with contextlib.closing(self._create_connection()) as connection:
                cursor = connection.cursor()
                cursor.execute(
                    "SELECT lastUpdated FROM Miner WHERE hotkey = ?", [miner_hotkey]
                )
                result = cursor.fetchone()
                if result is not None:
                    return result[0]
                else:
                    return None

    def read_databox_miners(self) -> List[DataBoxMiner]:
        """Gets details about miners for use in databox dashboards."""
        databox_miners = []

        with self.lock:
            # TODO consider doing this in a single cursor and a subquery.
            with contextlib.closing(self._create_connection()) as connection:
                cursor = connection.cursor()
                cursor.execute(
                    """SELECT hotkey, credibility, COUNT(*),
                            SUM(CASE WHEN source = 1 THEN contentSizeBytes ELSE 0 END),
                            SUM(CASE WHEN source = 2 THEN contentSizeBytes ELSE 0 END),
                            lastUpdated
                    FROM Miner
                    LEFT JOIN MinerIndex USING (minerId)
                    GROUP BY minerId"""
                )

                for row in cursor:
                    databox_miners.append(
                        DataBoxMiner(
                            hotkey=row[0],
                            credibility=row[1],
                            bucket_count=row[2],
                            content_size_bytes_reddit=row[3],
                            content_size_bytes_twitter=row[4],
                            last_updated=row[5],
                        )
                    )

            return databox_miners

    def read_databox_age_sizes(self) -> List[DataBoxAgeSize]:
        """Gets details about age sizes for use in databox dashboards."""

        # Only get top 1k per source due to databox limits.
        databox_age_sizes = []

        with self.lock:
            with contextlib.closing(self._create_connection()) as connection:
                cursor = connection.cursor()
                cursor.execute(
                    """SELECT timeBucketId, SUM(contentSizeBytes), SUM(contentSizeBytes * credibility) as adjSize
                    FROM Miner
                    LEFT JOIN MinerIndex USING (minerId)
                    WHERE source = 1
                    GROUP BY timeBucketId
                    ORDER BY adjSize DESC
                    LIMIT 1000"""
                )

                for row in cursor:
                    databox_age_sizes.append(
                        DataBoxAgeSize(
                            source=1,  # Get reddit first
                            time_bucket_id=row[0],
                            content_size_bytes=row[1],
                            adj_content_size_bytes=int(row[2]),
                        )
                    )

                cursor.execute(
                    """SELECT timeBucketId, SUM(contentSizeBytes), SUM(contentSizeBytes * credibility) as adjSize
                    FROM Miner
                    LEFT JOIN MinerIndex USING (minerId)
                    WHERE source = 2
                    GROUP BY timeBucketId
                    ORDER BY adjSize DESC
                    LIMIT 1000"""
                )

                for row in cursor:
                    databox_age_sizes.append(
                        DataBoxAgeSize(
                            source=2,  # Get X second
                            time_bucket_id=row[0],
                            content_size_bytes=row[1],
                            adj_content_size_bytes=int(row[2]),
                        )
                    )

            return databox_age_sizes

    def read_databox_label_sizes(self) -> List[DataBoxLabelSize]:
        """Gets details about label sizes for use in databox dashboards."""

        # Only get top 1k per source due to databox limits.
        databox_label_sizes = []

        with self.lock:
            with contextlib.closing(self._create_connection()) as connection:
                cursor = connection.cursor()
                cursor.execute(
                    """SELECT labelId, SUM(contentSizeBytes), SUM(contentSizeBytes * credibility) as adjSize
                    FROM Miner
                    LEFT JOIN MinerIndex USING (minerId)
                    WHERE source = 1
                    GROUP BY labelId
                    ORDER BY adjSize DESC
                    LIMIT 1000"""
                )

                for row in cursor:
                    databox_label_sizes.append(
                        DataBoxLabelSize(
                            source=1,  # Get reddit first
                            label_value=self.label_dict.get_by_id(row[0]),
                            content_size_bytes=row[1],
                            adj_content_size_bytes=int(row[2]),
                        )
                    )

                cursor.execute(
                    """SELECT labelId, SUM(contentSizeBytes), SUM(contentSizeBytes * credibility) as adjSize
                    FROM Miner
                    LEFT JOIN MinerIndex USING (minerId)
                    WHERE source = 2
                    GROUP BY labelId
                    ORDER BY adjSize DESC
                    LIMIT 1000"""
                )

                for row in cursor:
                    databox_label_sizes.append(
                        DataBoxLabelSize(
                            source=2,  # Get X second
                            label_value=self.label_dict.get_by_id(row[0]),
                            content_size_bytes=row[1],
                            adj_content_size_bytes=int(row[2]),
                        )
                    )

            return databox_label_sizes
