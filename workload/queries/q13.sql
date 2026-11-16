SELECT t.did, t.n FROM (SELECT did, COUNT(*) AS n FROM emp GROUP BY did) AS t WHERE t.n > 120;
