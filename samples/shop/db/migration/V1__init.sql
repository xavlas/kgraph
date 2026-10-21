CREATE TABLE customers (
    id           BIGSERIAL PRIMARY KEY,
    email        VARCHAR(200) NOT NULL UNIQUE,
    active       BOOLEAN NOT NULL DEFAULT TRUE,
    credit_limit NUMERIC(12,2) NOT NULL DEFAULT 5000 CHECK (credit_limit >= 0)
);

CREATE TABLE orders (
    id            BIGSERIAL PRIMARY KEY,
    customer_id   BIGINT NOT NULL REFERENCES customers(id),
    status        VARCHAR(20) NOT NULL,
    total_amount  NUMERIC(12,2) NOT NULL,
    created_at    TIMESTAMPTZ DEFAULT now(),
    CONSTRAINT chk_orders_status CHECK (status IN ('NEW','PAID','SHIPPED','CANCELLED'))
);

CREATE TABLE order_lines (
    id        BIGSERIAL PRIMARY KEY,
    order_id  BIGINT NOT NULL,
    sku       VARCHAR(40) NOT NULL,
    quantity  INT NOT NULL CHECK (quantity > 0),
    FOREIGN KEY (order_id) REFERENCES orders (id)
);

CREATE TABLE stock_items (
    sku           VARCHAR(40) PRIMARY KEY,
    available_qty INT NOT NULL CHECK (available_qty >= 0),
    reserved      INT NOT NULL DEFAULT 0
);

-- audit trail of order status changes
CREATE OR REPLACE FUNCTION log_order_status() RETURNS trigger AS $$
BEGIN
  INSERT INTO order_audit(order_id, status) VALUES (NEW.id, NEW.status);
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_order_status AFTER UPDATE ON orders FOR EACH ROW EXECUTE FUNCTION log_order_status();
