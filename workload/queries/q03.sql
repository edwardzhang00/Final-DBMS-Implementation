SELECT e.name, d.title FROM emp e, dept d WHERE e.did = d.did AND e.level = 5 AND d.region = 'East';
