REVOKE CONNECT ON DATABASE checkout_lab FROM PUBLIC;
CREATE ROLE checkout LOGIN PASSWORD 'lab-checkout' NOSUPERUSER;
CREATE ROLE worker LOGIN PASSWORD 'lab-worker' NOSUPERUSER;
GRANT CONNECT ON DATABASE checkout_lab TO checkout, worker;
CREATE TABLE checkout_probe (id integer PRIMARY KEY);
INSERT INTO checkout_probe VALUES (1);
GRANT SELECT ON checkout_probe TO checkout;
