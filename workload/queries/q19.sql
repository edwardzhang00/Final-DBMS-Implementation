SELECT e.city, COUNT(*) AS n FROM emp e JOIN dept d ON e.did = d.did WHERE e.city = d.city GROUP BY e.city ORDER BY n DESC, e.city;
