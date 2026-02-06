--
-- PostgreSQL database dump
--

\restrict UC48SCy2BYaZn7DbLiWr03tipYCECUpak2c3lr6GYSW1EDLbhMVQMRAe7Vum0SV

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
2	Емельяненко Михаил Михайлович	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.9	Лидер	1	1	49.50
3	Матус Антон Александрович	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.9	Лидер	1	1	44.50
4	Жданов Николай Федорович	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.9	Лидер	1	1	47.00
5	Косенко Екатерина Васильевна	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.9	Лидер	2	2	68.60
6	Лушников Анатолий Викторович	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.9	Лидер	1	1	66.40
7	Алискеров Тимур Шихрагимович	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.9	Лидер	1	1	63.50
8	Корепанов Андрей Васильевич	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.9	Лидер	1	1	65.00
9	Орлов Алексей Васильевич	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.9	Лидер	1	1	43.70
10	Лагутин Николай Алексеевич	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.9	Лидер	1	1	49.00
11	Семенцов Алексей Юрьевич	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.9	Лидер	1	1	45.80
12	Почекунин Александр Николаевич	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.9	Лидер	1	1	48.90
13	Бельков Юрий Иванович	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.9	Лидер	1	1	44.90
14	Осипов Павел Сергеевич	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.9	Лидер	1	1	66.20
15	Каримов Булат Ильфатович	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.9	Лидер	1	1	68.50
16	Мальков Дмитрий Николаевич	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.9	Лидер	1	1	60.30
17	Олейник Елена Александровна	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.9	ЦА	1	1	63.20
18	Карачевцев Алексей Анатольевич	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.9	Лидер	1	1	64.90
19	Покидин Николай Валентинович	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.9	ЦА	1	1	44.30
20	Лучка Александр Павлович	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.9	ЦА	2	2	64.90
21	Волков Владимир Игоревич	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.9	Лидер	1	1	47.80
22	Пушкарев Александр Вячеславович	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.9	Лидер	1	1	47.30
23	Резунов Роман Сергеевич	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.9	Лидер	3	3	47.10
24	Керн Ольга Алексеевна	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.9	Лидер	1	1	49.40
25	Пегарьков Александр Вячеславович	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.9	Лидер	1	1	47.90
26	Иваницкий Константин Васильевич	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.9	Лидер	1	1	64.70
27	Арцаев Руслан Султанович	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.9	Лидер	1	1	66.40
28	Вагидов Руслан Мирзалиевич	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.9	Лидер	1	1	66.80
29	Панасецкий Петр Петрович	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.9	Лидер	1	1	64.10
30	Панкратов Иван Дмитриевич	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.9	Лидер	1	1	45.80
31	Семченкова Валерия Андреевна	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.9	НЦУКС	3	3	64.30
32	Кешоков Азамат Ахмедович	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.9	Лидер	1	1	49.10
33	Душин Дмитрий Александрович	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.9	Лидер	1	1	47.90
34	Завацкий Алексей Николаевич	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.9	Лидер	1	1	45.90
35	Семенченко Никита Олегович	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.9	Лидер	1	1	46.40
36	Прокопенко Максим Борисович	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.9	Лидер	1	1	65.60
37	Шевченко Александр Александрович	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.9	Лидер	1	1	65.70
38	Черняев Максим Андреевич	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.9	Лидер	1	1	61.90
39	Смирнов Александр Александрович	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.9	Лидер	1	1	63.90
40	Мироненко Александр Михайлович	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.9	Лидер	1	1	44.50
41	Андросов Сергей Александрович	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.9	Лидер	1	1	64.90
42	Ильин Никита Юрьевич	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.9	Лидер	1	1	47.80
43	Ревин Денис Викторович	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.9	Лидер	1	1	47.40
44	Глушакова Анастасия Олеговна	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.9	Лидер	1	1	44.80
45	Еремин Павел Васильевич	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.9	Лидер	1	1	45.70
46	Резевский Виктор Викторович	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.9	Лидер	1	1	47.20
47	Айвазов Халил Кашалыевич	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.9	Лидер	1	1	65.30
48	Азимов Виталий Николаевич	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.9	Лидер	1	1	64.90
49	Таранюк Алексей Валерьевич	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.9	Лидер	1	1	61.90
50	Гюрджян Сергей Левонович	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.9	Лидер	1	1	64.80
51	Платов Денис Максимович	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.9	Лидер	1	1	44.50
52	Мурадов Руслан Фикретович	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.9	Лидер	1	1	65.20
53	Мурнаев Никита Евгеньевич	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.9	Лидер	1	1	48.00
54	Дибирасулаев Тимур Магамедович	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.9	Лидер	1	1	48.40
55	Кузнецов Виталий Сергеевич	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.8	Лидер	1	1	46.30
56	Запарин Михаил Александрович	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.8	Лидер	1	1	47.30
57	Кудрявцев Владимир Дмитриевич	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.8	Лидер	1	1	32.30
58	Кулешов Максим Михайлович	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.8	Лидер	1	1	28.90
59	Юриков Даниил Андреевич	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.8	Лидер	1	1	30.50
60	Попков Максим Владимирович	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.8	Лидер	1	1	30.20
61	Рыбальченко Сергей Владимирович	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.8	Лидер	1	1	42.10
62	Атиков Илия Рафаэльевич	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.8	Лидер	1	1	30.20
63	Джиоев Георгий Владимирович	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.8	Лидер	1	1	15.10
64	Осипов Руслан Сергеевич	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.8	Лидер	1	1	47.30
65	Струкова Ольга Сергеевна	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.8	Лидер	1	1	45.60
66	Седякин Игорь Иванович	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.8	Лидер	1	1	48.10
67	Крючков Артем Геннадьевич	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.8	Лидер	1	1	29.00
68	Прохоренко Константин Дмитриевич	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.8	Лидер	1	1	31.10
69	Кармов Беслан Ризуанович	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.8	Лидер	1	1	31.70
70	Снегирева Людмила Валерьевна	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.8	Лидер	2	2	32.40
71	Хабибулин Данила Романович	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.8	Лидер	3	3	28.70
72	Нурмагамедов Абдулнасир Магомедович	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.8	Лидер	1	1	31.70
73	Дуденко Максим Игоревич	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.8	Лидер	1	1	32.00
74	Соловьев Евгений Витальевич	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.8	Лидер	1	1	31.80
75	Смирнов Максим Алексеевич	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.8	Лидер	1	1	30.90
76	Левшин Александр Алексеевич	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.8	Лидер	1	1	30.90
77	Закамалдин Андрей Владимирович	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.8	Лидер	1	1	45.60
78	Ярощук Александр Павлович	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.8	Лидер	1	1	30.50
79	Беленьких Андрей Николаевич	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.8	Лидер	1	1	45.80
80	Чернов Сергей Дмитриевич	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.8	Лидер	1	1	45.40
81	Чаптыков Роман Алексеевич	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.8	Лидер	1	1	49.90
82	Чистяков Николай Юрьевич	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.8	АД	3	3	29.00
83	Шорошева Наталья Валентиновна	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.8	Лидер	2	2	31.10
84	Муртазина Алия Мифтяхотдиновна	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.8	Лидер	1	1	32.40
85	Тихонова Оксана Игоревна	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.8	НТУ	3	3	32.70
86	Гребенькова Ксения Юрьевна	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.8	Лидер	1	1	29.30
87	Сабаткоев Казбек Аланович	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.8	Лидер	1	1	30.50
88	Колемагин Дмитрий Александрович	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.8	Лидер	1	1	31.60
89	Фахреев Альберт Тимурович	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.8	Лидер	1	1	33.70
90	Сорокин Александр Евгеньевич	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.8	Лидер	1	1	32.30
91	Сорокин Сергей Алексеевич	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.8	Лидер	1	1	32.20
92	Хизанашвили Михаил Ясонович	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.8	Лидер	1	1	32.10
93	Безродний Сергей Александрович	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.8	Лидер	1	1	31.20
94	Мясников Даниил Геннадьевич	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.8	Лидер	1	1	31.10
95	Косынкин Даниил Андреевич	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.8	Лидер	1	1	33.20
96	Данилов Дмитрий Владимирович	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.8	Лидер	1	1	45.60
97	Дронин Константин Николаевич	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.8	Лидер	1	1	29.90
98	Анюхин Роман Сергеевич	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.8	Лидер	1	1	45.80
99	Писарев Александр Дмитриевич	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.8	Лидер	1	1	44.60
100	Дегтярев Владислав Андреевич	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.8	Лидер	1	1	47.20
101	Подловилин Давид Витальевич	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.8	Лидер	3	3	27.70
102	Катунькина Иннеса Ивановна	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.8	Лидер	1	1	31.50
103	Грибач Виталий Владимирович	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.8	ЦА	1	1	31.20
104	Пинских Юрий Игоревич	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.8	Лидер	1	1	31.50
105	Бендас Вадим Родионович	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.8	Лидер	1	1	29.60
106	Тюрин Александр Сергеевич	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.8	Лидер	1	1	30.10
107	Куликов Сергей Дмитриевич	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.8	Лидер	1	1	30.60
108	Шуянов Иван Николаевич	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.8	Лидер	1	1	32.20
109	Вастаев Валерий Сидорович	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.8	Лидер	1	1	30.70
110	Лапатько Кирилл Андреевич	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.8	Лидер	1	1	32.10
111	Кочелаев Денис Алексеевич	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.8	Лидер	1	1	30.80
112	Приходченков Игорь Сергеевич	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.8	Лидер	1	1	31.50
113	Кондратьева Мария Александровна	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.8	Лидер	1	1	27.10
114	Степанков Дмитрий Алексеевич	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.8	Лидер	1	1	30.20
115	Ермолаев Игорь Сергеевич	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.8	Лидер	1	1	15.10
116	Рудь Руслан Рамазанович	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.8	Лидер	1	1	15.10
117	Смолин Кирилл Сергеевич	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.8	Лидер	1	1	29.10
118	Мальков Сергей Николаевич	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.8	Лидер	3	3	14.55
119	Тищенко Максим Александрович	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.8	Лидер	1	1	14.55
120	Хруберов Кирилл Игоревич	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.8	Лидер	1	1	33.10
121	Залитдинов Шарапутдин Залитдинович	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.8	Лидер	1	1	11.03
122	Война Владислав Сергеевич	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.8	Лидер	1	1	33.60
123	Лисник Кирилл Борисович	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.8	Лидер	1	1	8.40
124	Попелов Алексей Сергеевич	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.8	Лидер	1	1	8.40
125	Жуков Савелий Дмитриевич	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.8	Лидер	1	1	8.40
126	Серый Артем Вячеславович	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.8	Лидер	1	1	16.80
127	Тарасов Валерий Вячеславович	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.8	Лидер	1	1	16.80
128	Том Богдан Игоревич	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.8	Лидер	1	1	10.66
129	Кудинов Максим Юрьевич	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.8	Лидер	1	1	10.66
130	Арутюнян Арман Горович	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.8	Лидер	1	1	10.66
131	Смирнов Даниил Николаевич	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.8	Лидер	1	1	14.80
132	Чекалин Александр Владимирович	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.8	Лидер	1	1	34.00
133	Тер-Саакян Даниил Витальевич	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.8	Лидер	1	1	11.34
134	Легконогих Александр Алексеевич	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.8	Лидер	1	1	11.34
135	Югай Арсений Анатольевич	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.9	\N	1	1	19.64
136	Усатая Елена Павловна	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.7	Лидер	2	2	45.00
137	Никитина Екатерина Александровна	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.7	Лидер	2	2	36.00
138	Агуреев Валерий Сергеевич	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.7	Лидер	2	2	13.60
139	Бирюлькин Михаил Анатольевич	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.7	Лидер	2	2	36.00
140	Сивицкая Елена Олеговна	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.7	Лидер	2	2	37.00
141	Конопихин Евгений Леонидович	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.7	Лидер	2	2	16.00
142	Приходько Алена Игоревна	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.7	Лидер	1	1	31.00
143	Умнов Михаил Евгеньевич	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	ОК	Лидер	1	1	46.10
144	Меликов Санжарбек Ашуралиевич	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	ОК	Лидер	1	1	23.05
145	Матисон Игорь Андреевич	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.18	Лидер	1	1	61.80
146	Белов Иван Максимович	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.18	Лидер	1	1	8.83
147	Терехов Максим Сергеевич	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.18	Лидер	1	1	8.83
148	Рыбин Павел Васильевич	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	32дв.стр.90	Лидер	1	1	15.50
149	Гуков Эльдар Альбертович	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.18	Лидер	1	1	8.83
150	Прасков Николай Валентинович	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.18	Лидер	1	1	49.50
151	Власов Станислав Андреевич	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.18	Лидер	1	1	24.75
152	Очетов Евгений Леонтьевич	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.18	Лидер	1	1	50.30
153	Шаргаев Дмитрий Александрович	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.18	Лидер	1	1	16.77
154	Кондрашов Герман Александрович	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.18	Лидер	1	1	16.77
155	Гурин Дмитрий Владимирович	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.18	Лидер	1	1	44.80
156	Тихон Михаил Александрович	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.18	Лидер	1	1	14.94
157	Серебряков Валерий Аркадьевич	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	32дв.стр.90	Лидер	1	1	15.50
158	Муравьев Павел Дмитриевич	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.18	Лидер	1	1	44.90
159	Шелюков Виктор Александрович	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.18	Лидер	1	1	14.97
160	Мороз Артем Анатольевич	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.18	Лидер	1	1	15.95
161	Гасымов Рамиз Рассадин Оглы	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.18	\N	1	1	42.60
162	Акбашев Ильфат Ильдарович	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.18	Лидер	1	1	14.20
163	Соколов Илья Евгеньевич	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.18	Лидер	1	1	38.10
164	Аникин Альберт Сергеевич	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.18	Лидер	1	1	9.53
165	Буйнов Марк Геннадьевич	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.18	Лидер	1	1	35.20
166	Абдулкеримов Арсен Гайдарович	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.18	Лидер	1	1	11.74
167	Смирнов Руслан Алексеевич	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.18	Лидер	1	1	11.74
168	Хрипачев Андрей Сергеевич	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.5	Лидер	1	1	47.64
169	Устимов Роман Сергеевич	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.5	Лидер	1	1	50.72
170	Оболенская Кира Олеговна	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.5	Лидер	1	1	31.60
171	Никольский Дмитрий Михайлович	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.5	Лидер	1	1	31.70
172	Дарий Андрей Дмитриевич	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.5	Лидер	1	1	34.00
173	Солоденко Роман Владимирович	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.5	Лидер	1	1	46.90
174	Баскаков Андрей Дмитриевич	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.5	Лидер	1	1	31.70
175	Шаховский Дмитрий Дмитриевич	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.5	Лидер	1	1	30.50
176	Валкович Андрей Петрович	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.5	Лидер	1	1	34.30
177	Безруков Михаил Сергеевич	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.5	Лидер	1	1	32.40
178	Калачев Вадим Евгеньевич	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.5	Лидер	1	1	47.40
179	Токтаев Илья Олегович	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.5	Лидер	1	1	34.00
180	Муталиев Ахмед Русланович	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.5	Лидер	1	1	52.50
181	Бульдина Наждежда Юрьевна	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.5	ЦСИ	3	3	29.00
182	Семкина Вера Васильевна	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.5	Лидер	2	2	33.60
183	Михалева Алена Борисовна	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.5	Лидер	2	2	35.60
184	Кривушин Илья Александрович	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.5	Лидер	1	1	35.20
185	Квасникова Вера Михайловна	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.5	Лидер	2	2	35.70
186	Федоров Иван Михайлович	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.5	Лидер	1	1	34.20
187	Сафаров Сахиб Гаджибаба-оглу	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.5	Лидер	3	3	34.40
188	Саттарова Ряися Маратовна	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.5	Лидер	2	2	33.00
189	Сергиенко Оксана Геннадьевна	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.5	Лидер	1	1	45.00
190	Быков Захар Игоревич	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.5	Лидер	1	1	29.90
191	Ширяев Андрей Игоревич	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.5	Лидер	1	1	33.40
192	Смирнов Максим Евгеньевич	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.5	Лидер	1	1	48.30
193	Абукаров Марат Мурадович	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.5	Лидер	1	1	30.90
194	Халваши Георгий Малхазович	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.5	Лидер	1	1	30.20
195	Звезда Антон Андреевич	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.5	Лидер	1	1	33.30
196	Тверяхин Вадим Михайлович	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.5	Лидер	1	1	32.20
197	Кононов Степан Александрович	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.5	Лидер	1	1	32.70
198	Джапбаров Осман Тимирбулатович	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.5	Лидер	1	1	33.20
199	Шмыров Вадим Евгеньевич	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.5	Лидер	1	1	33.60
200	Сулейманов Халид Рамазанович	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.5	Лидер	2	2	28.90
201	Никитин Андрей Андреевич	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.5	Лидер	1	1	31.50
202	Марченко Алексей Иванович	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.5	Лидер	1	1	35.10
203	Гагиев Давид Роландович	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.5	Лидер	1	1	33.90
204	Науменко Сергей Алексеевич	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.5	Лидер	1	1	35.70
205	Никитин Алексей Андреевич	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.5	Лидер	1	1	35.30
206	Маняев Иван Александрович	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.5	Лидер	1	1	33.80
207	Степаненко Андрей Александрович	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.5	Лидер	1	1	33.00
208	Байков Николай Васильевич	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.5	Лидер	1	1	46.70
209	Теплоухов Никита Николаевич	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.5	Лидер	1	1	32.50
210	Мимонова Виктория Владимировна	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.5	Лидер	3	3	29.70
211	Шпаков Анатолий Владимирович	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.5	Лидер	1	1	33.30
212	Назаретян Мнацакан Генрихович	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.5	Лидер	2	2	48.70
213	Степанков Михаил Алексеевич	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.5	Лидер	1	1	32.00
214	Гареев Артур Радифович	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.5	НЦУКС	3	3	31.40
215	Ананьева Ирина Сергеевна	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.5	Лидер	2	2	34.20
216	Ланина Маргарита Сергеевна	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.5	Лидер	1	1	32.10
217	Селезнева Валентина Ивановна	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.5	Лидер	2	2	33.80
218	Неметуллаева Арзу Рафиковна	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.5	Лидер	2	2	33.70
219	Азязов Александр Александрович	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.5	Лидер	1	1	28.30
220	Шигапов Ришат Фаридович	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.5	Лидер	1	1	32.00
221	Григорян Евгений Александрович	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.5	Лидер	1	1	34.50
222	Курносов Евгений Александрович	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.5	Лидер	1	1	34.70
223	Шумилова Лилия Сергеевна	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.5	Лидер	1	1	33.10
224	Хайбуллин Артем Ильсурович	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.5	Лидер	1	1	32.50
225	Галко Семен Васильевич	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.5	Лидер	1	1	46.40
226	Похиленко Евгений Евгеньевич	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.5	Лидер	1	1	33.60
227	Якубов Мухаммед Абдусаломович	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.5	Лидер	1	1	29.80
228	Глоба Илья Константинович	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.5	Лидер	1	1	33.70
229	Ишниязов Шухрат Саидович	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.5	Лидер	1	1	49.70
230	Бекетов Никита Викторович	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.5	Лидер	1	1	30.00
231	Шиян Александр Андреевич	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.5	Лидер	1	1	31.10
232	Абдулмеджидов Руслан Рагимович	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.5	Лидер	1	1	34.00
233	Коблев Мурат Мухамедович	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.5	Лидер	1	1	17.00
234	Власенко Максим Евгеньевич	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.5	Лидер	1	1	49.20
235	Горшкова Ольга Викторовна	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.5	ДСФ	3	3	33.30
236	Федотов Антон Сергеевич	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.5	Лидер	1	1	44.20
237	Есин Александр Александрович	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.5	Лидер	1	1	32.80
238	Бареев Радик Маликович	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.5	Лидер	1	1	53.60
239	Матюхин Илья Владимирович	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.5	Лидер	1	1	65.40
240	Ленский Дмитрий Николаевич	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.5	Лидер	1	1	51.00
241	Каштанов Александр Андреевич	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.5	Лидер	1	1	33.50
242	Шустров Алексей Евгеньевич	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.5	Лидер	3	3	48.20
243	Забабурин Андрей Алексеевич	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.5	Лидер	1	1	33.70
244	Фомичев Павел Андреевич	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.5	Лидер	1	1	34.50
245	Дробков Юрий Сергеевич	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.5	Лидер	1	1	29.70
246	Матюхина Елена Сергеевна	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.15	Лидер	1	1	47.90
247	Терехов Александр Георгиевич	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.15	Лидер	1	1	47.50
248	Нежведилов Рамазан Сулейманович	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.15	Лидер	1	1	63.20
249	Каврук Олег Иванович	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.15	Лидер	1	1	60.50
250	Веселкин Евгений Вадимович	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.15	Лидер	1	1	70.90
251	Заславский Данила Евгеньевич	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.15	Лидер	1	1	11.82
252	Юрьев Денис Евгеньевич	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	32дв.стр.90	Лидер	1	1	46.50
253	Терещенко Георгий Александрович	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.15	Лидер	1	1	11.82
254	Шишкарев Иван Евгеньевич	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.15	Лидер	1	1	56.60
255	Тюнев Сергей Александрович	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.15	Лидер	1	1	55.80
256	Липша Сергей Сергеевич	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.15	Лидер	1	1	55.20
257	Черешнев Виталий Александрович	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.15	Лидер	1	1	55.20
258	Капранов Виталий Вячеславович	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.15	Лидер	1	1	55.30
259	Пушкарев Александр Валерьевич	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.15	Лидер	1	1	56.00
260	Дудов Ахмат Муратович	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.15	Лидер	1	1	55.50
261	Кудяков Никита Алексеевич	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.15	Лидер	1	1	38.60
262	Бачиев Назир Ахметович	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.15	Лидер	1	1	50.10
263	Давыдов Роман Михайлович	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.15	Лидер	1	1	16.70
264	Верхозин Артём Михайлович	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.15	Лидер	1	1	16.70
265	Зайцев Евгений Дмитриевич	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.15	Лидер	1	1	49.40
266	Нестеров Сергей Валерьевич	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.15	Лидер	1	1	65.10
267	Марченко Иван Иванович	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.15	Лидер	1	1	63.80
268	Агаметов Байрамали Гаджиевич	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.15	Лидер	1	1	71.30
269	Скабелин Сергей Николаевич	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.15	Лидер	1	1	56.80
270	Эсетов Вадим Джамидинович	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.15	Лидер	1	1	55.60
271	Магомедов Магомед Эльдарович	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.15	Лидер	1	1	56.50
272	Раджабов Багадур Мурадович	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.15	Лидер	1	1	56.10
273	Гилаев Рафаэль Равилевич	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.15	Лидер	1	1	56.30
274	Мухаметкулов Иван Евгеньевич	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.15	Лидер	1	1	55.70
275	Афанасьев Петр Олегович	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.15	Лидер	1	1	39.40
276	Близняков Иван Константинович	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	4дв.стр.15	Лидер	1	1	95.20
277	Колотухин Андрей Владимирович	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	32дв.стр.90	Лидер	1	1	31.50
278	Миронов Сергей Юрьевич	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	32дв.стр.90	Лидер	1	1	29.20
279	Кирюшин Кирилл Михайлович	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	32дв.стр.90	Лидер	1	1	14.60
280	Нестерчук Денис Петрович	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	32дв.стр.90	АП	3	3	29.00
281	Хайдуков Руслан Геннадьевич	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	32дв.стр.90	Лидер	1	1	32.20
282	Ганиев Ариф Ариф	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	32дв.стр.90	Лидер	1	1	49.20
283	Локтионов Евгений Александрович	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	32дв.стр.90	Лидер	1	1	56.80
284	Колокоцкий Алексей Александрович	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	32дв.стр.90	Лидер	1	1	49.10
285	Кулешов Даниил Владимирович	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	32дв.стр.90	Лидер	3	3	75.00
286	Капорин Евгений Вадимович	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	32дв.стр.90	Лидер	1	1	50.00
287	Атаян Георгий Сергеевич	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	32дв.стр.90	Лидер	3	3	31.50
288	Нижиндаева Виктория Викторовна	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	32дв.стр.90	ДКП	3	3	33.50
289	Новикова Ирина Олеговна	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	32дв.стр.90	ДСФ	3	3	31.50
290	Вилданов Даниэль Данисович	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	32дв.стр.90	Лидер	1	1	29.60
291	Мащицкий Анатолий Олегович	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	32дв.стр.90	ДНД	3	3	31.90
292	Галяс Юрий Владимирович	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	32дв.стр.90	Лидер	3	3	70.90
293	Яцук Татьяна Михайловна	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	32дв.стр.90	Лидер	1	1	31.90
294	Гамзатов Рамиз Шахбанович	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	32дв.стр.90	Лидер	1	1	45.90
295	Илюшина Алиса Михайловна	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	32дв.стр.90	АПУ	3	3	56.40
296	Щетинин Александар Михайлович	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	32дв.стр.90	МТО	3	3	50.50
297	Полохин Павел Александрович	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	32дв.стр.90	Лидер	1	1	48.20
298	Рязанова Екатерина Николаевна	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	32дв.стр.90	Лидер	1	1	73.70
299	Карпов Юрий Иванович	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	32дв.стр.90	УВГСЧ	3	3	48.90
300	Зайцев Дмитрий Михайлович	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	32дв.стр.90	Лидер	1	1	31.00
301	Бондарь Олег Павлович	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	32дв.стр.90	ДТО	3	3	55.80
302	Бачиев Алим Ахметович	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	32дв.стр.90	Лидер	1	1	32.50
303	Егоровцева Олеся Витальевна	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	32дв.стр.90	УИС	3	3	47.10
304	Половников Олег Сергеевич	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	32дв.стр.90	Лидер	1	1	21.70
305	Кубанский Александр Викторович	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	32дв.стр.90	ДГО	3	3	32.50
306	Марченко Алексей Николаевич	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	32дв.стр.90	Лидер	1	1	41.40
307	Карпюк Дмитрий Викторович	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	32дв.стр.90	НЦУКС	3	3	75.70
308	Бакаев Даниил Дмитриевич	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	32дв.стр.90	Лидер	3	3	59.30
309	Федосеев Алексей Сергеевич	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	32дв.стр.90	ЦА	3	3	51.90
310	Костерев Дмитрий Михайлович	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	32дв.стр.90	Лидер	1	1	53.20
311	Петров Иван Владимирович	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	32дв.стр.90	Лидер	3	3	52.70
312	Кузьминов Сергей Иванович	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	32дв.стр.90	Лидер	1	1	52.50
313	Сурин Анатолий Евгеньевич	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	32дв.стр.90	УИС	3	3	51.10
314	Романов Виктор Юрьевич	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	32дв.стр.90	Лидер	1	1	34.10
315	Гордеев Михаил Владимирович	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	32дв.стр.90	Лидер	1	1	56.80
316	Захаров Виталий Сергеевич	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	32дв.стр.90	Лидер	1	1	58.20
317	Малышкин Антон Викторович	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	32дв.стр.90	ЦУКС	3	3	44.40
318	Неуймин Владислав Андреевич	$2b$12$2.fdQy42n17LLrfUGc16PeTCo2acB6DiaZ9dK6TN/GVsUnhhp67F2	user	32дв.стр.90	ДТО	3	3	40.20
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

SELECT pg_catalog.setval('public.users_id_seq', 318, true);


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

\unrestrict UC48SCy2BYaZn7DbLiWr03tipYCECUpak2c3lr6GYSW1EDLbhMVQMRAe7Vum0SV

