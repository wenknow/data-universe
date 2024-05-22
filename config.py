import sqlite3
import json

# 连接到SQLite数据库，如果文件不存在则会创建
conn = sqlite3.connect('SqliteMinerStorage.sqlite')
cursor = conn.cursor()

# 创建表
cursor.execute("""
CREATE TABLE IF NOT EXISTS ScrapyConfig (
    label CHAR(32) COLLATE NOCASE PRIMARY KEY,
    source INTEGER NOT NULL,
    minutes INTEGER NOT NULL,
    size INTEGER NOT NULL,
    count INTEGER NOT NULL,
    rate INTEGER NOT NULL,
    uptime TIMESTAMP(6) NOT NULL,
    bytes INTEGER NOT NULL default 0
)
""")

# 读取JSON文件数据
with open('config.json', 'r', encoding='utf-8') as file:
    configs = json.load(file)

# 将JSON数据插入到表中
for config in configs:
    cursor.execute("""
    INSERT INTO ScrapyConfig (label, source,minutes,size, count, rate, uptime)
    VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (config['label'], config['source'], config['minutes'], config['size'], config['count'], config['rate'], config['uptime']))

# 提交事务
conn.commit()

# 关闭连接
conn.close()