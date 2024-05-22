import sqlite3


# 连接到SQLite数据库，如果文件不存在则会创建
conn = sqlite3.connect('SqliteMinerStorage.sqlite')
cursor = conn.cursor()

cursor.execute("SELECT SUM(contentSizeBytes),label FROM DataEntity where source=1 group by label")
rows = cursor.fetchall()
for row in rows:
    cursor.execute(f"UPDATE ScrapyConfig SET bytes = ? WHERE label = ?", (row[0], row[1]))

cursor.execute("SELECT SUM(contentSizeBytes) FROM DataEntity")
all_bytes = cursor.fetchone()[0]

cursor.execute("SELECT SUM(bytes) FROM ScrapyConfig")
config_bytes = cursor.fetchone()[0]

cursor.execute("REPLACE INTO ScrapyConfig VALUES (?,?,?,?,?,?,?,?)", ('', 0,0,0,0,0,'2024-05-15 01:22:37+00:00', all_bytes-config_bytes))

# 提交事务
conn.commit()

# 关闭连接
conn.close()
