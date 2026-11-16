-- Workload schema. The same statements run unchanged on PostgreSQL.
CREATE TABLE dept (did INT, title VARCHAR(20), budget INT, city VARCHAR(20), region VARCHAR(10));
CREATE TABLE emp (eid INT, name VARCHAR(20), age INT, did INT, salary INT, level INT, city VARCHAR(20), mgr INT);
CREATE TABLE project (pid INT, did INT, title VARCHAR(24), budget INT);
CREATE TABLE works (eid INT, pid INT, hours INT);
