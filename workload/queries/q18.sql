SELECT level, MIN(salary) AS lo, MAX(salary) AS hi, COUNT(*) AS n FROM emp GROUP BY level ORDER BY level;
