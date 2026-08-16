import sqlite3
conn = sqlite3.connect('edgedash.db')
conn.row_factory = sqlite3.Row
rows = conn.execute(
    'SELECT id, fit_score, title, company FROM listings '
    'WHERE fit_score IS NOT NULL ORDER BY fit_score DESC'
).fetchall()
print(f'{len(rows)} scored listings\n')
print(f"  {'SCORE':>5}  {'TITLE':<45}  COMPANY")
print(f"  {'-----':>5}  {'-'*45}  -------")
for r in rows:
    print(f"  {r['fit_score']:>5}  {str(r['title'])[:45]:<45}  {r['company']}")
