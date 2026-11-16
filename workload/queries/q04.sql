SELECT d.title, COUNT(*) AS n, AVG(e.salary) AS avg_sal FROM emp e JOIN dept d ON e.did = d.did GROUP BY d.title HAVING COUNT(*) > 100 ORDER BY n DESC, d.title LIMIT 10;
