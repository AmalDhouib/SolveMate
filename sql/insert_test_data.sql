-- =====================================================
-- SolveMate : données de test (scénarios de la section 10 du cahier des charges)
-- Script rejouable : il vide les tables puis réinsère un jeu cohérent.
-- Les dates sont relatives à NOW() pour que les scénarios restent valides
-- quel que soit le jour d'exécution (ex. panne de Brest commencée il y a 3 h).
-- =====================================================

TRUNCATE crm.agent_memory, crm.tickets, billing.payments, billing.invoices,
         oss.network_incidents, crm.customers
RESTART IDENTITY CASCADE;

-- ---------------------- CLIENTS ----------------------
-- customer_id attribués dans l'ordre : 1 Alice ... 7 Lina, 8 Sophie
INSERT INTO crm.customers (name, email, phone_number, subscription_type, network_zone) VALUES
('Alice Martin',   'alice.martin@example.com',   '0601010101', 'Fibre',  'Brest'),
('Bruno Lefevre',  'bruno.lefevre@example.com',  '0602020202', 'Fibre',  'Rennes'),
('Chloé Bernard',  'chloe.bernard@example.com',  '0603030303', 'ADSL',   'Nantes'),
('David Moreau',   'david.moreau@example.com',   '0604040404', 'Mobile', 'Lyon'),
('Emma Petit',     'emma.petit@example.com',     '0605050505', 'Fibre',  'Paris'),
('Hugo Roux',      'hugo.roux@example.com',      '0606060606', 'Fibre',  'Toulouse'),
('Lina Garcia',    'lina.garcia@example.com',    '0607070707', 'Fibre',  'Brest'),
('Sophie Laurent', 'sophie.laurent@example.com', '0608080808', 'Fibre',  'Marseille');

-- ---------------------- FACTURES ----------------------
INSERT INTO billing.invoices (customer_id, amount, due_date, status) VALUES
-- Alice : tout est payé
(1, 39.99, CURRENT_DATE - 40, 'Paid'),
(1, 39.99, CURRENT_DATE - 10, 'Paid'),
-- Bruno : paiement Completed mais facture restée Unpaid (anomalie)
(2, 44.99, CURRENT_DATE - 38, 'Paid'),
(2, 44.99, CURRENT_DATE - 8,  'Unpaid'),
-- Chloé : paiement refusé, facture en retard
(3, 29.99, CURRENT_DATE - 35, 'Paid'),
(3, 29.99, CURRENT_DATE - 5,  'Overdue'),
-- David : facture non échue, paiement Pending d'hier
(4, 19.99, CURRENT_DATE + 5,  'Unpaid'),
-- Emma : tout est payé
(5, 49.99, CURRENT_DATE - 42, 'Paid'),
(5, 49.99, CURRENT_DATE - 12, 'Paid'),
-- Hugo : facture en retard, aucun paiement enregistré
(6, 39.99, CURRENT_DATE - 15, 'Overdue'),
-- Lina : paiement partiel
(7, 59.99, CURRENT_DATE - 3,  'Unpaid'),
-- Sophie : tout est payé (client sans scénario, sert aux tests d'isolation)
(8, 34.99, CURRENT_DATE - 20, 'Paid');

-- ---------------------- PAIEMENTS ----------------------
-- invoice_id : 1-2 Alice, 3-4 Bruno, 5-6 Chloé, 7 David, 8-9 Emma, 10 Hugo, 11 Lina, 12 Sophie
INSERT INTO billing.payments (invoice_id, payment_date, amount, status) VALUES
(1,  NOW() - INTERVAL '41 days', 39.99, 'Completed'),
(2,  NOW() - INTERVAL '11 days', 39.99, 'Completed'),
(3,  NOW() - INTERVAL '39 days', 44.99, 'Completed'),
(4,  NOW() - INTERVAL '6 days',  44.99, 'Completed'),
(5,  NOW() - INTERVAL '36 days', 29.99, 'Completed'),
(6,  NOW() - INTERVAL '4 days',  29.99, 'Failed'),
(7,  NOW() - INTERVAL '1 day',   19.99, 'Pending'),
(8,  NOW() - INTERVAL '43 days', 49.99, 'Completed'),
(9,  NOW() - INTERVAL '13 days', 49.99, 'Completed'),
(11, NOW() - INTERVAL '2 days',  30.00, 'Completed'),
(12, NOW() - INTERVAL '21 days', 34.99, 'Completed');

-- ---------------------- INCIDENTS RÉSEAU ----------------------
INSERT INTO oss.network_incidents (affected_area, incident_type, started_at, resolved_at, status, description) VALUES
('Brest',  'Panne fibre optique', NOW() - INTERVAL '3 hours', NULL, 'Ongoing',
 'Coupure d''un câble fibre principal. Équipes techniques sur place.'),
('Rennes', 'Maintenance planifiée', NOW() - INTERVAL '5 days', NOW() - INTERVAL '5 days' + INTERVAL '4 hours', 'Resolved',
 'Maintenance nocturne terminée.'),
('Paris',  'Coupure électrique antenne', NOW() - INTERVAL '6 days', NOW() - INTERVAL '6 days' + INTERVAL '2 hours', 'Resolved',
 'Incident résolu, sans lien avec la situation actuelle.'),
('Marseille', 'Saturation réseau mobile', NOW() - INTERVAL '30 hours', NOW() - INTERVAL '20 hours', 'Resolved',
 'Saturation lors d''un événement, résolue.');

-- ---------------------- TICKETS ----------------------
INSERT INTO crm.tickets (customer_id, category, created_at, status, priority, description) VALUES
-- Alice a déjà un ticket Network ouvert pour la panne de Brest
(1, 'Network', NOW() - INTERVAL '2 hours', 'Open', 'Low',
 'Client sans Internet, panne fibre en cours à Brest.'),
-- Hugo : dossier Billing ouvert, en attente du justificatif
(6, 'Billing', NOW() - INTERVAL '2 days', 'Open', 'Medium',
 'Client affirme avoir payé, aucun paiement trouvé. Justificatif demandé.'),
-- Historique fermé
(5, 'Service', NOW() - INTERVAL '60 days', 'Closed', 'Low',
 'Question sur les options de l''abonnement. Répondu.');

-- ---------------------- MÉMOIRE DE L'AGENT ----------------------
INSERT INTO crm.agent_memory (customer_id, ticket_id, email_subject, summary, action_taken, state, created_at) VALUES
(6, 2, 'Facture payée mais compte en retard',
 'Le client affirme avoir payé sa facture en retard de 39,99 EUR. Aucun paiement trouvé. Un justificatif de paiement (reçu ou relevé bancaire) lui a été demandé.',
 'Demande de justificatif envoyée au client', 'WaitingCustomer', NOW() - INTERVAL '2 days'),
(5, 3, 'Question sur mon abonnement',
 'Question générale sur les options de l''abonnement Fibre. Réponse envoyée.',
 'Réponse envoyée au client', 'Resolved', NOW() - INTERVAL '60 days');
