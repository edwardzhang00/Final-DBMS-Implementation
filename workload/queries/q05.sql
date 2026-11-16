SELECT e.name, m.name FROM emp e JOIN emp m ON e.mgr = m.eid WHERE e.salary > m.salary AND e.level = 1;
