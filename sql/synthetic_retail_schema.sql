PRAGMA foreign_keys = ON;

-- Project-created schema for linked synthetic customers. ABCD does not supply
-- customer IDs or these relational constraints.
CREATE TABLE customers (
    customer_id TEXT PRIMARY KEY,
    customer_name TEXT NOT NULL UNIQUE,
    account_id TEXT NOT NULL UNIQUE,
    username TEXT NOT NULL UNIQUE,
    email TEXT NOT NULL UNIQUE,
    phone TEXT NOT NULL UNIQUE,
    member_level TEXT NOT NULL,
    split TEXT NOT NULL,
    identity_source TEXT NOT NULL
);

CREATE TABLE orders (
    order_id TEXT PRIMARY KEY,
    customer_id TEXT NOT NULL REFERENCES customers(customer_id),
    source_convo_id INTEGER NOT NULL UNIQUE,
    purchase_date TEXT,
    shipping_status TEXT,
    shipping_status_source TEXT NOT NULL,
    packaging TEXT,
    payment_method TEXT,
    num_products INTEGER NOT NULL,
    full_address TEXT,
    street_address TEXT,
    city TEXT,
    state TEXT,
    zip_code TEXT
);

CREATE TABLE items (
    order_id TEXT NOT NULL REFERENCES orders(order_id),
    item_index INTEGER NOT NULL,
    name TEXT,
    amount REAL,
    brand TEXT,
    product_type TEXT,
    image_url TEXT,
    PRIMARY KEY (order_id, item_index)
);

CREATE TABLE source_cases (
    source_convo_id INTEGER PRIMARY KEY,
    split TEXT NOT NULL,
    subflow TEXT NOT NULL,
    customer_id TEXT NOT NULL REFERENCES customers(customer_id),
    order_id TEXT NOT NULL REFERENCES orders(order_id),
    source_kind TEXT NOT NULL,
    reference_label_status TEXT NOT NULL,
    review_flags TEXT NOT NULL
);

CREATE INDEX orders_by_customer ON orders(customer_id);

CREATE VIEW order_shipping_lookup AS
    SELECT c.account_id, o.order_id, o.shipping_status, o.shipping_status_source
    FROM orders o JOIN customers c ON c.customer_id = o.customer_id;
