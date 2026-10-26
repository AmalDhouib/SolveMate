-- SolveMate : création des schémas et des tables

CREATE SCHEMA IF NOT EXISTS crm;
CREATE SCHEMA IF NOT EXISTS billing;
CREATE SCHEMA IF NOT EXISTS oss;

-- ---------------------- CRM ----------------------

CREATE TABLE IF NOT EXISTS crm.customers (
    customer_id SERIAL PRIMARY KEY,
    name VARCHAR(100),
    email VARCHAR(150) UNIQUE NOT NULL,
    phone_number VARCHAR(20),
    subscription_type VARCHAR(50),
    -- Lien logique avec oss.network_incidents.affected_area
    network_zone VARCHAR(100)
);


CREATE TABLE IF NOT EXISTS crm.tickets (
    ticket_id SERIAL PRIMARY KEY,
    customer_id INT NOT NULL,
    category VARCHAR(50) NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    status VARCHAR(20) DEFAULT 'Open',
    priority VARCHAR(10) DEFAULT 'Medium',
    description TEXT,

    CONSTRAINT chk_ticket_category
        CHECK (category IN ('Billing', 'Network', 'Service')),

    CONSTRAINT chk_ticket_status
        CHECK (status IN ('Open', 'In Progress', 'Closed')),

    CONSTRAINT chk_ticket_priority
        CHECK (priority IN ('Low', 'Medium', 'High')),

    CONSTRAINT fk_ticket_customer
        FOREIGN KEY (customer_id)
        REFERENCES crm.customers(customer_id)
);

CREATE TABLE IF NOT EXISTS crm.agent_memory (
    memory_id SERIAL PRIMARY KEY,
    customer_id INT NOT NULL,
    ticket_id INT,
    email_subject VARCHAR(200),
    summary TEXT NOT NULL,
    action_taken VARCHAR(100),
    state VARCHAR(30) DEFAULT 'Resolved',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT chk_memory_state
        CHECK (state IN ('WaitingCustomer', 'Escalated', 'Resolved')),

    CONSTRAINT fk_memory_customer
        FOREIGN KEY (customer_id)
        REFERENCES crm.customers(customer_id),

    CONSTRAINT fk_memory_ticket
        FOREIGN KEY (ticket_id)
        REFERENCES crm.tickets(ticket_id)
);

-- ---------------------- BILLING ----------------------

CREATE TABLE IF NOT EXISTS billing.invoices (
    invoice_id SERIAL PRIMARY KEY,
    customer_id INT NOT NULL,
    amount NUMERIC(10,2) NOT NULL,
    due_date DATE NOT NULL,
    status VARCHAR(20) DEFAULT 'Unpaid',

    CONSTRAINT chk_invoice_amount
        CHECK (amount >= 0),

    CONSTRAINT chk_invoice_status
        CHECK (status IN ('Paid', 'Unpaid', 'Overdue')),

    CONSTRAINT fk_invoice_customer
        FOREIGN KEY (customer_id)
        REFERENCES crm.customers(customer_id)
);

CREATE TABLE IF NOT EXISTS billing.payments (
    payment_id SERIAL PRIMARY KEY,
    invoice_id INT NOT NULL,
    payment_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    amount NUMERIC(10,2) NOT NULL,
    status VARCHAR(20) DEFAULT 'Pending',

    CONSTRAINT chk_payment_amount
        CHECK (amount > 0),

    CONSTRAINT chk_payment_status
        CHECK (status IN ('Pending', 'Completed', 'Failed')),

    CONSTRAINT fk_payment_invoice
        FOREIGN KEY (invoice_id)
        REFERENCES billing.invoices(invoice_id)
);

-- ---------------------- OSS ----------------------

CREATE TABLE IF NOT EXISTS oss.network_incidents (
    incident_id SERIAL PRIMARY KEY,
    affected_area VARCHAR(100) NOT NULL,
    incident_type VARCHAR(100) NOT NULL,
    started_at TIMESTAMP NOT NULL,
    resolved_at TIMESTAMP,
    status VARCHAR(20) DEFAULT 'Ongoing',
    description TEXT,

    CONSTRAINT chk_incident_status
        CHECK (status IN ('Ongoing', 'Resolved')),

    CONSTRAINT chk_incident_dates
        CHECK (
            resolved_at IS NULL
            OR resolved_at >= started_at
        )
);

-- ---------------------- INDEX ----------------------

CREATE INDEX IF NOT EXISTS idx_tickets_customer ON crm.tickets(customer_id, status);
CREATE INDEX IF NOT EXISTS idx_memory_customer ON crm.agent_memory(customer_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_invoices_customer ON billing.invoices(customer_id);
CREATE INDEX IF NOT EXISTS idx_payments_invoice ON billing.payments(invoice_id);
CREATE INDEX IF NOT EXISTS idx_incidents_area ON oss.network_incidents(affected_area, status);
