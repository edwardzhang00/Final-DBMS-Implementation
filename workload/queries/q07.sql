SELECT e.name, p.title, w.hours FROM emp e, works w, project p WHERE e.eid = w.eid AND w.pid = p.pid AND e.age < 22 AND p.budget > 500000;
