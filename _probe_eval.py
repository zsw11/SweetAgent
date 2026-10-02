"""调研 evaluation 三表现状：数据从哪来、何时插入。"""
import psycopg

conn = psycopg.connect(
    "postgresql://app_user:app_password@localhost:5432/sweetnight_agent",
    connect_timeout=5,
)

print("== evaluation_cases 统计 ==")
n = conn.execute("SELECT count(*), min(created_at), max(created_at) FROM evaluation_cases").fetchone()
print("count:", n[0], "| min_created:", n[1], "| max_created:", n[2])
rows = conn.execute(
    "SELECT id, category, question, created_at FROM evaluation_cases ORDER BY id LIMIT 25"
).fetchall()
for r in rows:
    print(" case", r[0], "|", r[1], "|", r[2][:36], "|", r[3])

print()
print("== evaluation_runs 统计 ==")
n = conn.execute("SELECT count(*), min(started_at), max(started_at) FROM evaluation_runs").fetchone()
print("count:", n[0], "| min:", n[1], "| max:", n[2])
rows = conn.execute(
    "SELECT id, run_id, status, started_at, finished_at FROM evaluation_runs ORDER BY id DESC LIMIT 8"
).fetchall()
for r in rows:
    print(" run", r[0], "|", r[1], "|", r[2], "|", r[3], "->", r[4])

print()
print("== evaluation_scores 统计 ==")
n = conn.execute("SELECT count(*), count(DISTINCT run_id), count(DISTINCT case_id) FROM evaluation_scores").fetchone()
print("score_rows:", n[0], "| distinct_runs:", n[1], "| distinct_cases:", n[2])
rows = conn.execute(
    "SELECT metric, count(*), min(created_at), max(created_at) FROM evaluation_scores GROUP BY metric ORDER BY metric"
).fetchall()
for r in rows:
    print(" metric", r[0], "|", r[1], "条 |", r[2], "->", r[3])

conn.close()
