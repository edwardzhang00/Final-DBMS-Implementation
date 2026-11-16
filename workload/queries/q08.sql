SELECT d.region, COUNT(*) AS n FROM emp e JOIN dept d ON e.did = d.did JOIN works w ON w.eid = e.eid JOIN project p ON p.pid = w.pid WHERE p.budget < 100000 GROUP BY d.region ORDER BY d.region;
