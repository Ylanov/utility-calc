--
-- PostgreSQL database dump
--

\restrict ZBhLv11ysdsa1LJJ4Lr5va6TjP7aLgmE0uGdyQdvkVpHQvoRsFmCFGlhqtLXeFD

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
    hot_water double precision,
    cold_water double precision,
    electricity double precision,
    hot_correction double precision,
    cold_correction double precision,
    electricity_correction double precision,
    sewage_correction double precision,
    total_cost double precision,
    cost_hot_water double precision,
    cost_cold_water double precision,
    cost_electricity double precision,
    cost_sewage double precision,
    cost_maintenance double precision,
    cost_fixed_part double precision,
    is_approved boolean,
    created_at timestamp without time zone,
    cost_social_rent double precision DEFAULT 0.0,
    cost_waste double precision DEFAULT 0.0,
    period_id integer,
    anomaly_flags character varying
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
    maintenance_repair double precision,
    social_rent double precision,
    heating double precision,
    water_heating double precision,
    water_supply double precision,
    sewage double precision,
    waste_disposal double precision,
    electricity_per_sqm double precision,
    electricity_rate double precision
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
    apartment_area double precision
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
899a92e54307
\.


--
-- Data for Name: periods; Type: TABLE DATA; Schema: public; Owner: postgres
--

COPY public.periods (id, name, is_active, created_at) FROM stdin;
1	Начальный период	f	2026-02-01 17:52:03.018515
\.


--
-- Data for Name: readings; Type: TABLE DATA; Schema: public; Owner: postgres
--

COPY public.readings (id, user_id, hot_water, cold_water, electricity, hot_correction, cold_correction, electricity_correction, sewage_correction, total_cost, cost_hot_water, cost_cold_water, cost_electricity, cost_sewage, cost_maintenance, cost_fixed_part, is_approved, created_at, cost_social_rent, cost_waste, period_id, anomaly_flags) FROM stdin;
1	1	117	264	20197	0	0	0	0	211931.78	32671.08	16415.52	144610.52	18234.66	0	0	t	2026-01-31 11:29:47.633362	0	0	\N	\N
3	2	117	264	20197	0	0	0	0	212920.29	32671.08	16415.52	144610.52	18234.66	988.5	0	t	2026-01-31 11:33:14.259896	0	0	\N	\N
4	2	119	268	20197	0	0	0	0	2082.86	558.48	248.72	0	287.16	988.5	0	t	2026-01-31 14:03:57.327443	0	0	\N	\N
2	1	119	268	20197	0	0	0	0	1094.36	558.48	248.72	0	287.16	0	0	t	2026-01-31 11:32:26.683472	0	0	\N	\N
5	1	122	278	20280	0	0	0	0	2675.98	837.72	621.8	594.28	622.18	0	0	t	2026-02-01 18:31:21.384593	0	0	1	\N
6	2	127	277	20250	0	0	0	0	6088.39	2233.92	559.62	379.48	813.62	988.5	0	t	2026-02-01 18:32:03.337734	856.13	257.12	1	\N
7	1	127	299	20555	0	0	0	0	5915.34	1396.2	1305.78	1969	1244.36	0	0	t	2026-02-01 18:37:16.56997	0	0	1	\N
9	2	133	299	20500	0	0	0	0	8275.24	1675.44	1367.96	1790	1340.08	988.5	0	t	2026-02-01 18:57:47.825909	856.13	257.12	1	\N
10	2	155	340	20800	0	0	0	0	15957.59	6143.28	2549.38	2148	3015.18	988.5	0	t	2026-02-05 09:24:20.331419	856.13	257.12	1	HIGH_COLD,HIGH_HOT
8	1	145	307	20700	0	0	0	0	7806.32	5026.32	497.44	1038.2	1244.36	0	0	t	2026-02-01 18:57:16.954704	0	0	1	\N
\.


--
-- Data for Name: tariffs; Type: TABLE DATA; Schema: public; Owner: postgres
--

COPY public.tariffs (id, maintenance_repair, social_rent, heating, water_heating, water_supply, sewage, waste_disposal, electricity_per_sqm, electricity_rate) FROM stdin;
1	32.41	28.07	0	217.06	62.18	47.86	8.43	0	7.16
\.


--
-- Data for Name: users; Type: TABLE DATA; Schema: public; Owner: postgres
--

COPY public.users (id, username, hashed_password, role, dormitory, workplace, residents_count, total_room_residents, apartment_area) FROM stdin;
1	admin	$2b$12$DPND99Dbn8N9PGG9jW2ohuJZ2NYDrgaRCVItt0JAF9KhmqE84G9CC	accountant	\N	\N	1	1	0
2	Ярощук	$2b$12$TJ1TAPN93QLoKkG2di0jGuen6D.PHr698eZc6CvehheiQjIm9jIwq	user	4	Лидер	2	2	30.5
\.


--
-- Name: periods_id_seq; Type: SEQUENCE SET; Schema: public; Owner: postgres
--

SELECT pg_catalog.setval('public.periods_id_seq', 1, true);


--
-- Name: readings_id_seq; Type: SEQUENCE SET; Schema: public; Owner: postgres
--

SELECT pg_catalog.setval('public.readings_id_seq', 10, true);


--
-- Name: tariffs_id_seq; Type: SEQUENCE SET; Schema: public; Owner: postgres
--

SELECT pg_catalog.setval('public.tariffs_id_seq', 1, false);


--
-- Name: users_id_seq; Type: SEQUENCE SET; Schema: public; Owner: postgres
--

SELECT pg_catalog.setval('public.users_id_seq', 2, true);


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
    ADD CONSTRAINT readings_period_id_fkey FOREIGN KEY (period_id) REFERENCES public.periods(id) ON DELETE SET NULL;


--
-- Name: readings readings_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.readings
    ADD CONSTRAINT readings_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;


--
-- PostgreSQL database dump complete
--

\unrestrict ZBhLv11ysdsa1LJJ4Lr5va6TjP7aLgmE0uGdyQdvkVpHQvoRsFmCFGlhqtLXeFD

