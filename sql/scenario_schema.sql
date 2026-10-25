PRAGMA foreign_keys = ON;

-- ABCD reuses fictional names. convo_id isolates each scenario; it is never
-- exposed to the agent as a customer identifier.
CREATE TABLE scenarios (
    convo_id INTEGER PRIMARY KEY,
    split TEXT NOT NULL CHECK (split IN ('train', 'dev', 'test')),
    flow TEXT NOT NULL,
    subflow TEXT NOT NULL
);

CREATE TABLE accounts (
    convo_id INTEGER PRIMARY KEY REFERENCES scenarios(convo_id),
    customer_name TEXT,
    account_id TEXT,
    username TEXT,
    email TEXT,
    phone TEXT,
    member_level TEXT
);

CREATE TABLE orders (
    convo_id INTEGER PRIMARY KEY REFERENCES scenarios(convo_id),
    order_id TEXT NOT NULL,
    num_products INTEGER NOT NULL CHECK (num_products >= 0),
    purchase_date TEXT,
    shipping_status TEXT,
    packaging TEXT,
    payment_method TEXT,
    street_address TEXT,
    full_address TEXT,
    city TEXT,
    state TEXT,
    zip_code TEXT
);

CREATE TABLE products (
    convo_id INTEGER NOT NULL REFERENCES scenarios(convo_id),
    item_index INTEGER NOT NULL CHECK (item_index >= 0),
    name TEXT,
    brand TEXT,
    product_type TEXT,
    amount REAL,
    image_url TEXT,
    PRIMARY KEY (convo_id, item_index)
);

CREATE INDEX accounts_by_name ON accounts(convo_id, customer_name);
CREATE INDEX accounts_by_account_id ON accounts(convo_id, account_id);
CREATE INDEX orders_by_order_id ON orders(convo_id, order_id);
