SELECT t.did, t.avg_sal, d.title FROM (SELECT did, AVG(salary) AS avg_sal FROM emp WHERE level = 4 GROUP BY did) t JOIN dept d ON t.did = d.did WHERE d.budget > 50000;
