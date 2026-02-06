--
-- PostgreSQL database dump
--

\restrict hI6F6eAs3seULOZOS4giFXan0onP81GiwuXc1WcaW4xADQoDw32j1JWuyc4U3vl

-- Dumped from database version 15.15 (Debian 15.15-1.pgdg13+1)
-- Dumped by pg_dump version 15.15 (Debian 15.15-1.pgdg13+1)

SET statement_timeout = 0;
SET lock_timeout = 0;
SET idle_in_transaction_session_timeout = 0;
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
SELECT pg_catalog.set_config('search_path', '', false);
SET check_function_bodies = false;
SET xmloption = content;
SET client_min_messages = warning;
SET row_security = off;

SET default_tablespace = '';

SET default_table_access_method = heap;

--
-- Name: alembic_version; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.alembic_version (
    version_num character varying(32) NOT NULL
);


ALTER TABLE public.alembic_version OWNER TO postgres;

--
-- Name: periods; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.periods (
    id integer NOT NULL,
    name character varying,
    is_active boolean,
    created_at timestamp without time zone
);


ALTER TABLE public.periods OWNER TO postgres;

--
-- Name: periods_id_seq; Type: SEQUENCE; Schema: public; Owner: postgres
--

CREATE SEQUENCE public.periods_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER TABLE public.periods_id_seq OWNER TO postgres;

--
-- Name: periods_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: postgres
--

ALTER SEQUENCE public.periods_id_seq OWNED BY public.periods.id;


--
-- Name: readings; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.readings (
    id integer NOT NULL,
    user_id integer,
    period_id integer,
    hot_water numeric(12,3),
    cold_water numeric(12,3),
    electricity numeric(12,3),
    hot_correction numeric(12,3),
    cold_correction numeric(12,3),
    electricity_correction numeric(12,3),
    sewage_correction numeric(12,3),
    total_cost numeric(12,2),
    cost_hot_water numeric(12,2),
    cost_cold_water numeric(12,2),
    cost_electricity numeric(12,2),
    cost_sewage numeric(12,2),
    cost_maintenance numeric(12,2),
    cost_social_rent numeric(12,2),
    cost_waste numeric(12,2),
    cost_fixed_part numeric(12,2),
    anomaly_flags character varying,
    is_approved boolean,
    created_at timestamp without time zone
);


ALTER TABLE public.readings OWNER TO postgres;

--
-- Name: readings_id_seq; Type: SEQUENCE; Schema: public; Owner: postgres
--

CREATE SEQUENCE public.readings_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER TABLE public.readings_id_seq OWNER TO postgres;

--
-- Name: readings_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: postgres
--

ALTER SEQUENCE public.readings_id_seq OWNED BY public.readings.id;


--
-- Name: tariffs; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.tariffs (
    id integer NOT NULL,
    maintenance_repair numeric(10,4),
    social_rent numeric(10,4),
    heating numeric(10,4),
    water_heating numeric(10,4),
    water_supply numeric(10,4),
    sewage numeric(10,4),
    waste_disposal numeric(10,4),
    electricity_per_sqm numeric(10,4),
    electricity_rate numeric(10,4)
);


ALTER TABLE public.tariffs OWNER TO postgres;

--
-- Name: tariffs_id_seq; Type: SEQUENCE; Schema: public; Owner: postgres
--

CREATE SEQUENCE public.tariffs_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER TABLE public.tariffs_id_seq OWNER TO postgres;

--
-- Name: tariffs_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: postgres
--

ALTER SEQUENCE public.tariffs_id_seq OWNED BY public.tariffs.id;


--
-- Name: users; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.users (
    id integer NOT NULL,
    username character varying,
    hashed_password character varying,
    role character varying,
    dormitory character varying,
    workplace character varying,
    residents_count integer,
    total_room_residents integer,
    apartment_area numeric(10,2)
);


ALTER TABLE public.users OWNER TO postgres;

--
-- Name: users_id_seq; Type: SEQUENCE; Schema: public; Owner: postgres
--

CREATE SEQUENCE public.users_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER TABLE public.users_id_seq OWNER TO postgres;

--
-- Name: users_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: postgres
--

ALTER SEQUENCE public.users_id_seq OWNED BY public.users.id;


--
-- Name: periods id; Type: DEFAULT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.periods ALTER COLUMN id SET DEFAULT nextval('public.periods_id_seq'::regclass);


--
-- Name: readings id; Type: DEFAULT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.readings ALTER COLUMN id SET DEFAULT nextval('public.readings_id_seq'::regclass);


--
-- Name: tariffs id; Type: DEFAULT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.tariffs ALTER COLUMN id SET DEFAULT nextval('public.tariffs_id_seq'::regclass);


--
-- Name: users id; Type: DEFAULT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.users ALTER COLUMN id SET DEFAULT nextval('public.users_id_seq'::regclass);


--
-- Data for Name: alembic_version; Type: TABLE DATA; Schema: public; Owner: postgres
--

COPY public.alembic_version (version_num) FROM stdin;
\.


--
-- Data for Name: periods; Type: TABLE DATA; Schema: public; Owner: postgres
--

COPY public.periods (id, name, is_active, created_at) FROM stdin;
1	Начальный период	t	2026-02-06 06:37:34.311901
\.


--
-- Data for Name: readings; Type: TABLE DATA; Schema: public; Owner: postgres
--

COPY public.readings (id, user_id, period_id, hot_water, cold_water, electricity, hot_correction, cold_correction, electricity_correction, sewage_correction, total_cost, cost_hot_water, cost_cold_water, cost_electricity, cost_sewage, cost_maintenance, cost_social_rent, cost_waste, cost_fixed_part, anomaly_flags, is_approved, created_at) FROM stdin;
\.


--
-- Data for Name: tariffs; Type: TABLE DATA; Schema: public; Owner: postgres
--

COPY public.tariffs (id, maintenance_repair, social_rent, heating, water_heating, water_supply, sewage, waste_disposal, electricity_per_sqm, electricity_rate) FROM stdin;
1	0.0000	0.0000	0.0000	0.0000	0.0000	0.0000	0.0000	0.0000	5.0000
\.


--
-- Data for Name: users; Type: TABLE DATA; Schema: public; Owner: postgres
--

COPY public.users (id, username, hashed_password, role, dormitory, workplace, residents_count, total_room_residents, apartment_area) FROM stdin;
1	admin	$2b$12$J0ej3VStzRG7Z4O/tZIwSOYHpRSbqBu4hMwrrli7cHzutiw8LtAnS	accountant	\N	\N	1	1	0.00
\.


--
-- Name: periods_id_seq; Type: SEQUENCE SET; Schema: public; Owner: postgres
--

SELECT pg_catalog.setval('public.periods_id_seq', 1, true);


--
-- Name: readings_id_seq; Type: SEQUENCE SET; Schema: public; Owner: postgres
--

SELECT pg_catalog.setval('public.readings_id_seq', 1, false);


--
-- Name: tariffs_id_seq; Type: SEQUENCE SET; Schema: public; Owner: postgres
--

SELECT pg_catalog.setval('public.tariffs_id_seq', 1, false);


--
-- Name: users_id_seq; Type: SEQUENCE SET; Schema: public; Owner: postgres
--

SELECT pg_catalog.setval('public.users_id_seq', 1, true);


--
-- Name: alembic_version alembic_version_pkc; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.alembic_version
    ADD CONSTRAINT alembic_version_pkc PRIMARY KEY (version_num);


--
-- Name: periods periods_name_key; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.periods
    ADD CONSTRAINT periods_name_key UNIQUE (name);


--
-- Name: periods periods_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.periods
    ADD CONSTRAINT periods_pkey PRIMARY KEY (id);


--
-- Name: readings readings_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.readings
    ADD CONSTRAINT readings_pkey PRIMARY KEY (id);


--
-- Name: tariffs tariffs_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.tariffs
    ADD CONSTRAINT tariffs_pkey PRIMARY KEY (id);


--
-- Name: users users_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.users
    ADD CONSTRAINT users_pkey PRIMARY KEY (id);


--
-- Name: idx_approved_period; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX idx_approved_period ON public.readings USING btree (is_approved, period_id);


--
-- Name: idx_user_period; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX idx_user_period ON public.readings USING btree (user_id, period_id);


--
-- Name: ix_periods_id; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX ix_periods_id ON public.periods USING btree (id);


--
-- Name: ix_readings_id; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX ix_readings_id ON public.readings USING btree (id);


--
-- Name: ix_users_dormitory; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX ix_users_dormitory ON public.users USING btree (dormitory);


--
-- Name: ix_users_id; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX ix_users_id ON public.users USING btree (id);


--
-- Name: ix_users_username; Type: INDEX; Schema: public; Owner: postgres
--

CREATE UNIQUE INDEX ix_users_username ON public.users USING btree (username);


--
-- Name: readings readings_period_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.readings
    ADD CONSTRAINT readings_period_id_fkey FOREIGN KEY (period_id) REFERENCES public.periods(id);


--
-- Name: readings readings_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.readings
    ADD CONSTRAINT readings_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id);


--
-- PostgreSQL database dump complete
--

\unrestrict hI6F6eAs3seULOZOS4giFXan0onP81GiwuXc1WcaW4xADQoDw32j1JWuyc4U3vl

