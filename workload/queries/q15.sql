SELECT e.name, e.city, w.hours FROM emp e JOIN works w ON e.eid = w.eid WHERE e.level = 5 AND e.salary >= 105000;
