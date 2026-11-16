SELECT p.title, SUM(w.hours) AS total FROM project p JOIN works w ON p.pid = w.pid GROUP BY p.title ORDER BY total DESC, p.title LIMIT 5;
