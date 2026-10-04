"""Наполняет базу демо-лидами за последние 30 дней: python3 seed_demo.py (CRM_DB задаёт путь к базе).
Нужен, чтобы показать интерфейс с данными. Все лиды получают тег «Демо», по нему их можно найти и удалить разом."""
import random
from datetime import datetime, timedelta, timezone

import db

random.seed(7)
PEOPLE = [
    ("Мария Соколова", "+7 999 123-45-67 (@msokolova)", "Нужна реклама в Instagram для студии йоги, бюджет 80 тысяч в месяц", "bot"),
    ("Пётр Анисимов", "@petr_a", "Добрый день, нужен логотип и фирменный стиль для пекарни", "telegram"),
    ("ООО «Нордлайн»", "info@nordline.example", "Сайт-каталог на 300 позиций с выгрузкой из 1С", "web"),
    ("Олег Громов", "oleg@example.com", "Сайт-визитка для адвоката, срочно", "manual"),
    ("Анна Ветрова", "+7 921 555-01-02", "SMM-ведение двух аккаунтов, нужна контент-стратегия", "bot"),
    ("Кофейня «Зерно»", "@zerno_coffee", "Продвижение в картах и 2ГИС, запуск через 2 недели", "telegram"),
    ("Игорь Левин", "levin@example.com", "Таргет ВКонтакте для автосервиса", "web"),
    ("Светлана К.", "+7 904 777-88-99", "Видеоролики для рекламы, 5 штук по 30 секунд", "bot"),
    ("Дмитрий Орлов", "@orlov_d", "Лендинг под курс по Python и настройка Метрики", "telegram"),
    ("Фитнес-клуб «Ритм»", "club@ritm.example", "Контекстная реклама Яндекс Директ", "web"),
    ("Елена Беляева", "+7 912 300-40-50", "Нужен сайт для цветочного магазина", "bot"),
    ("Никита Р.", "@nikita_rr", "Фотосессия товаров для маркетплейса, 120 позиций", "manual"),
]
RULES = [("сайт", "Разработка"), ("лендинг", "Разработка"), ("реклам", "Реклама"), ("smm", "SMM"), ("логотип", "Дизайн"), ("видео", "Видео")]
STATUSES = ["new"] * 4 + ["in_work"] * 4 + ["won"] * 2 + ["lost"]
MANAGERS = ["Анна", "Максим"]

FIRST = ["Алексей", "Ирина", "Сергей", "Ольга", "Андрей", "Наталья", "Павел", "Елена", "Роман", "Татьяна", "Виктор", "Юлия",
         "Антон", "Марина", "Глеб", "Дарья", "Кирилл", "Вера"]
HANDLES = ["alex", "irina", "sergey", "olga", "andrey", "nata", "pavel", "elena", "roman", "tanya", "viktor", "julia"]


def extra_person(source):
    """Человеческое имя и контакт для лидов сверх первых двенадцати: «Ирина К.», @irina_42, почта или телефон."""
    first, initial = random.choice(FIRST), random.choice("АБВГДЕЖЗИКЛМНОПРСТ")
    handle = f"{random.choice(HANDLES)}_{random.randint(10, 99)}"
    if source in ("bot", "telegram"):
        contact = f"@{handle}"
    elif random.random() < .5:
        contact = f"{handle}@example.com"
    else:
        contact = f"+7 9{random.randint(10, 99)} {random.randint(100, 999)}-{random.randint(10, 99)}-{random.randint(10, 99)}"
    return f"{first} {initial}.", contact


have_rules = {(r["keyword"], r["tag"]) for r in db.list_rules()}
for k, t in RULES:
    if (k, t) not in have_rules:  # повторный запуск не плодит одинаковые правила
        db.add_rule(k, t)
for m in MANAGERS:
    db.add_manager(m)
now = datetime.now(timezone.utc)
for i in range(46):
    name, contact, request, source = PEOPLE[i % len(PEOPLE)]
    if i >= len(PEOPLE):
        name, contact = extra_person(source)
    # Telegram-идентификаторов у демо-лидов нет специально: ответить из CRM им нельзя, ни один настоящий чат не заденется.
    # Тег «Демо» отличает образцы от настоящих заявок: фильтр по нему + «выбрать всех» + «удалить» убирает их разом.
    lead = db.create_lead(name, contact, request, source, tags=["Демо"], first_message=request if source == "telegram" else None)
    status = random.choice(STATUSES)
    patch = {"status": status}
    if random.random() < .7:
        patch["assignee"] = random.choice(MANAGERS)
    if random.random() < .6:
        patch["amount"] = random.choice([30000, 45000, 80000, 120000, 250000])
    if status in ("new", "in_work") and random.random() < .6:
        patch["due_at"] = (now + timedelta(days=random.randint(-3, 6))).date().isoformat()
    if random.random() < .25:
        patch["tags"] = [t["name"] for t in lead["tags"]] + [random.choice(["Горячий", "VIP", "Повторный"])]
    db.update_lead(lead["id"], patch)
    created = (now - timedelta(days=random.choices(range(30), weights=[6] + [3] * 7 + [2] * 22)[0], hours=random.randint(0, 23))).strftime("%Y-%m-%dT%H:%M:%SZ")
    with db.conn() as c:
        c.execute("UPDATE leads SET created_at=?, updated_at=? WHERE id=?", (created, created, lead["id"]))
        c.execute("UPDATE events SET ts=? WHERE lead_id=?", (created, lead["id"]))
        if source == "telegram":
            c.execute("UPDATE leads SET unread=? WHERE id=?", (random.choice([0, 0, 1, 2]), lead["id"]))
print("готово:", len(db.list_leads()), "лидов")
