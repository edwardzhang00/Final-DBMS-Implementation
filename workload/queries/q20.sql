SELECT p.title, e.name FROM project p JOIN works w ON p.pid = w.pid JOIN emp e ON e.eid = w.eid WHERE w.hours > 38 AND e.level = 2 ORDER BY p.title, e.name LIMIT 20;
