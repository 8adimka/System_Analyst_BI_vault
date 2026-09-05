# Epic: Проработка и реализация мультиарендной архитектуры Modus BI Cloud для массового SaaS-сегмента

## Тип задачи
Epic / Architecture & Platform Enhancement

---

# Название задачи
Проработка и реализация безопасной мультиарендной архитектуры Modus BI Cloud для SMB/Freemium-сегмента

---

# Бизнес-контекст

На текущий момент продукт Modus BI существует в двух вариантах поставки:

1. **On-Premise**
   - заказчик разворачивает инфраструктуру в собственном контуре;
   - самостоятельно управляет СУБД, ETL-процессами и хранением данных;
   - решение ориентировано на Enterprise-сегмент с повышенными требованиями к безопасности и наличием собственной ИТ-команды.

2. **Modus BI Cloud**
   - облачная SaaS-версия платформы;
   - инфраструктура развёрнута на стороне Modus BI;
   - заказчики регистрируются самостоятельно;
   - данные хранятся и обрабатываются в общей инфраструктуре;
   - решение ориентировано на SMB, малый бизнес, физлиц и Freemium-сегмент.

Текущая реализация облачной версии носит пилотный характер и требует системного проектирования мультиарендной архитектуры (multi-tenant architecture), обеспечивающей:

- безопасную изоляцию клиентов;
- масштабируемость;
- минимальную стоимость владения;
- простоту эксплуатации;
- минимальные трудозатраты на разработку и поддержку;
- возможность массового подключения новых клиентов.

Ключевое ограничение:

> Целевым приоритетом является массовый рынок (SMB/Freemium), а не Enterprise-сегмент.

Соответственно, архитектура должна быть экономически эффективной и масштабируемой.

---

# Цель задачи

Спроектировать и реализовать целевую мультиарендную архитектуру Modus BI Cloud с приоритетом:

- массового подключения клиентов;
- минимизации инфраструктурных затрат;
- обеспечения достаточного уровня безопасности;
- снижения риска межтенантных утечек данных;
- обеспечения HighLoad-готовности ClickHouse;
- возможности дальнейшего расширения до Enterprise-режима.

---

# Основная проблема

Текущая облачная архитектура требует определения стратегии изоляции tenant-данных.

Необходимо выбрать и формализовать:

- способ хранения данных клиентов;
- уровень изоляции;
- механизмы безопасности;
- архитектурные ограничения;
- модель масштабирования;
- модель эксплуатации.

Ключевой конфликт:

- высокая степень изоляции существенно увеличивает стоимость инфраструктуры и сложность сопровождения;
- высокая степень консолидации снижает стоимость, но увеличивает риски утечек данных и noisy neighbor-эффект.

Необходимо определить компромиссное решение, оптимальное для SaaS-модели массового сегмента.

---

# Текущее состояние платформы

## Технологический стек

### Portal / Metadata Layer
- PostgreSQL
- хранение:
  - пользователей;
  - workspace;
  - метаданных;
  - настроек;
  - дашбордов;
  - прав;
  - конфигураций.

### Analytical Storage
- ClickHouse
- хранение аналитических данных пользователей.

### Frontend
- React

### Data Sources
Поддерживаемые/планируемые источники:
- Excel;
- Google Sheets;
- CSV (требует уточнения);
- 1С;
- внешние БД;
- ETL-пайплайны.

---

# Рассматриваемые архитектурные варианты

# Вариант 1 — Логическая изоляция
## Shared Database + Shared Schema + Row-Level Security

---

## Описание

Все клиенты используют:

- общую инфраструктуру;
- общие БД;
- общие таблицы.

Изоляция обеспечивается логически.

Во все сущности добавляется:

- tenant_id;
- workspace_id.

### PostgreSQL
Изоляция реализуется через:

- PostgreSQL Row-Level Security (RLS);
- Security Policies;
- ограничение доступа на уровне ролей;
- дополнительную фильтрацию в backend/ORM.

### ClickHouse
Изоляция реализуется через:

- tenant_id в начале Primary Key / ORDER BY;
- Row Policies;
- фильтрацию запросов;
- обязательное tenant-aware проектирование запросов.

---

## Преимущества

### Экономическая эффективность

Максимально эффективное использование инфраструктуры:

- одна БД;
- единый кластер;
- минимальный overhead;
- подходит для массового рынка.

### Масштабируемость

ClickHouse оптимально работает:

- с малым количеством больших таблиц;
- с крупными партициями;
- с высокой степенью консолидации.

### Простота DevOps и миграций

- единый ALTER TABLE;
- единый pipeline;
- единая схема обновления;
- отсутствие необходимости массовой оркестрации tenant-инстансов.

### Минимальные трудозатраты

Подход требует существенно меньших изменений платформы.

---

## Недостатки и риски

### Риск межтенантных утечек

Основной риск:

> человеческая ошибка в backend/query layer.

Пример:

- отсутствует WHERE tenant_id;
- некорректный JOIN;
- обход ORM;
- выполнение raw SQL;
- неправильная политика безопасности.

Следствие:

- один клиент получает доступ к данным другого клиента.

### Noisy Neighbor Effect

Тяжёлые запросы одного tenant могут:

- деградировать производительность;
- занимать ресурсы ClickHouse;
- влиять на latency других клиентов.

### Ограничения Enterprise-сегмента

Некоторые заказчики:

- не допускают shared infrastructure;
- требуют полного физического разделения;
- требуют отдельного security perimeter.

---

## Вывод по варианту

Вариант является:

- наиболее реалистичным;
- экономически оправданным;
- приоритетным для SaaS-модели Modus BI Cloud.

При условии:

- обязательного внедрения RLS/Policies;
- централизованного tenant-aware access layer;
- запрета bypass-механизмов;
- аудита запросов;
- автоматизированного тестирования изоляции.

---

# Вариант 2 — Физическая изоляция
## Dedicated Infrastructure / Pod per Tenant

---

## Описание

Для каждого tenant/workspace разворачивается:

- отдельный backend instance;
- отдельный Postgres;
- отдельный ClickHouse;
- отдельные контейнеры/Pods.

Оркестрация выполняется через Kubernetes.

---

## Преимущества

### Максимальная безопасность

Полная физическая изоляция:

- отсутствие shared storage;
- отсутствие shared query execution;
- отсутствие риска межтенантного чтения.

### Гибкость

Возможность:

- обновлять клиентов отдельно;
- использовать разные версии платформы;
- индивидуально масштабировать tenant.

### Enterprise-ready

Подход соответствует требованиям:

- Enterprise;
- regulated sectors;
- security-sensitive клиентов.

---

## Недостатки и риски

### Критически высокая стоимость

Недостатки:

- высокий инфраструктурный overhead;
- дорогая эксплуатация;
- дорогой storage;
- сложный autoscaling.

Подход экономически неэффективен для:

- Freemium;
- SMB;
- малого объёма данных.

### Сложность оркестрации

Потребуется:

- сложная Kubernetes-оркестрация;
- управление stateful workloads;
- автоматическое provisioning;
- lifecycle management;
- tenant deployment automation.

### Высокие трудозатраты разработки

Необходима реализация:

- orchestration layer;
- tenant provisioning;
- tenant lifecycle;
- routing;
- monitoring;
- backup isolation;
- multi-cluster management.

---

## Вывод по варианту

Вариант:

- нецелесообразен для массового SaaS-сегмента;
- противоречит задаче минимизации стоимости;
- может быть применим только для Enterprise/VIP-тарифа.

Дополнительно требуется оценка:

> имеет ли Dedicated Cloud смысл относительно существующего On-Premise-подхода.

Возможен сценарий, при котором:

- дешевле;
- безопаснее;
- проще сопровождать

будет именно классический On-Premise.

---

# Гибридный вариант (предпочтительный для исследования)

---

# Идея

Использовать комбинированную модель:

## PostgreSQL
Физическая/логическая сегментация метаданных:

Возможные варианты:

- schema-per-tenant;
- database-per-tenant;
- isolated metadata tables;
- tenant-specific schemas.

### Причина

PostgreSQL нормально работает:

- с большим количеством таблиц;
- схем;
- metadata isolation.

Это позволяет:

- усилить безопасность;
- снизить риск утечки критичных данных;
- упростить разграничение прав.

---

## ClickHouse

Оставить:

- shared database;
- shared tables;
- tenant_id-based isolation.

### Причина

ClickHouse плохо масштабируется:

- на тысячи мелких таблиц;
- на большое количество isolated datasets.

Наиболее эффективный сценарий:

- небольшое количество крупных таблиц.

---

## Потенциальные преимущества гибридной модели

- существенно повышенная безопасность;
- ограничение blast radius;
- лучшая управляемость metadata layer;
- сохранение производительности ClickHouse;
- умеренное увеличение сложности;
- минимальный рост инфраструктурных затрат.

---

## Потенциальные недостатки

- усложнение архитектуры;
- усложнение migration pipelines;
- необходимость синхронизации tenant lifecycle;
- усложнение backup/recovery.

---

## Предварительный вывод

Гибридный вариант выглядит:

- наиболее компромиссным;
- потенциально оптимальным;
- заслуживающим детального технического исследования.

---

# Целевое архитектурное направление

На текущем этапе целевым направлением считать:

## Основной режим

### Shared Infrastructure

С обязательными механизмами:

- tenant isolation;
- RLS;
- Row Policies;
- централизованного access layer;
- tenant-aware query execution;
- ограничений на raw SQL;
- аудита доступа.

---

## Дополнительный режим

### Enterprise Isolation Mode

Рассматривать как:

- отдельный тариф;
- дополнительную опцию;
- non-MVP capability.

---

# Требования к реализации

# Функциональные требования

## FR-1. Tenant Isolation

Система должна обеспечивать логическую изоляцию данных между tenant/workspace.

---

## FR-2. Tenant Context

Каждый запрос к:

- PostgreSQL;
- ClickHouse;
- backend services

должен выполняться в tenant-context.

---

## FR-3. Mandatory Tenant Binding

Все tenant-sensitive сущности обязаны содержать:

- tenant_id;
- workspace_id.

---

## FR-4. RLS Support

Для PostgreSQL должны быть реализованы:

- Row-Level Security Policies;
- tenant-scoped roles;
- автоматическая tenant-фильтрация.

---

## FR-5. ClickHouse Isolation

Для ClickHouse должны быть реализованы:

- Row Policies;
- tenant-aware queries;
- tenant_id в ORDER BY / PRIMARY KEY.

---

## FR-6. Tenant Lifecycle

Система должна поддерживать:

- создание tenant;
- удаление tenant;
- деактивацию tenant;
- quota management.

---

## FR-7. Security Audit

Необходимо логирование:

- межтенантных обращений;
- security violations;
- access denied событий.

---

## FR-8. Query Protection

Должны быть исключены механизмы bypass tenant isolation:

- raw SQL без tenant context;
- direct table access;
- service-level bypass.

---

## FR-9. Rate Limiting / Resource Isolation

Должны быть реализованы ограничения:

- по CPU;
- по памяти;
- по query execution;
- по concurrency.

Для минимизации noisy neighbor-эффекта.

---

# Нефункциональные требования

## NFR-1. Scalability

Архитектура должна поддерживать:

- массовое подключение SMB/Freemium клиентов;
- горизонтальное масштабирование;
- рост объёма tenant-данных.

---

## NFR-2. Performance

Tenant isolation не должна создавать критическую деградацию производительности.

---

## NFR-3. Security

Не допускается доступ tenant к данным другого tenant.

---

## NFR-4. Maintainability

Архитектура должна быть:

- поддерживаемой;
- расширяемой;
- пригодной для централизованных миграций.

---

## NFR-5. Cost Efficiency

Решение должно быть экономически эффективно для SMB/Freemium.

---

# Предположения

1. Основной рынок — SMB/Freemium.
2. Enterprise-клиенты продолжат использовать On-Premise.
3. Полная физическая изоляция не является обязательной для MVP.
4. ClickHouse остаётся основной аналитической СУБД.
5. PostgreSQL остаётся metadata storage.
6. Архитектура должна быть совместима с существующим portal/backend.
7. Tenant lifecycle должен быть автоматизирован.
8. Использование Kubernetes допускается, но не должно становиться обязательным условием MVP.

---

# Открытые вопросы

## Архитектурные

1. Используется ли сейчас tenant_id во всех ключевых сущностях?
2. Насколько backend готов к tenant-aware execution?
3. Есть ли raw SQL, обходящий ORM/access layer?
4. Есть ли централизованный query builder?
5. Есть ли поддержка RLS в текущем persistence layer?
6. Используется ли шардинг ClickHouse?
7. Планируется ли multi-region deployment?

---

## Эксплуатационные

1. Как будет реализован backup/restore tenant?
2. Как будет происходить tenant offboarding?
3. Нужна ли tenant-level observability?
4. Требуется ли отдельный billing/quota subsystem?

---

## Security

1. Какие compliance-требования ожидаются?
2. Требуется ли encryption-per-tenant?
3. Требуется ли tenant-specific secret management?

---

## Product/Business

1. Планируется ли Enterprise Cloud тариф?
2. Нужен ли dedicated deployment option?
3. Где проходит граница между Cloud Enterprise и On-Premise?

---

# Технические рекомендации

## PostgreSQL

Рекомендуется:

- обязательный RLS;
- запрет прямого доступа к таблицам;
- использование security-definer procedures;
- tenant-aware repositories/services;
- audit logging.

---

## ClickHouse

Рекомендуется:

- tenant_id в ORDER BY;
- partition strategy с учётом tenant;
- Row Policies;
- query quotas;
- workload isolation;
- query governor.

---

## Backend

Рекомендуется:

- централизованный Tenant Context;
- middleware-инъекция tenant;
- запрет bypass tenant layer;
- обязательный security review data-access logic.

---

# Этапы реализации

## Этап 1 — Исследование

- аудит текущей архитектуры;
- аудит tenant isolation;
- аудит SQL access;
- анализ ClickHouse схем;
- анализ metadata layer.

Результат:
- Architecture Decision Record (ADR).

---

## Этап 2 — Проектирование

- проектирование tenant model;
- проектирование RLS;
- проектирование access layer;
- проектирование quotas;
- проектирование hybrid approach.

---

## Этап 3 — MVP Implementation

- tenant_id integration;
- RLS implementation;
- Row Policies;
- tenant-aware backend;
- observability.

---

## Этап 4 — Hardening

- penetration testing;
- isolation testing;
- load testing;
- noisy neighbor testing.

---

# Критерии приёмки (Acceptance Criteria)

## AC-1

Все tenant-sensitive таблицы содержат:

- tenant_id;
- либо workspace_id.

---

## AC-2

Для PostgreSQL реализованы и включены:

- Row-Level Security;
- Security Policies.

---

## AC-3

Для ClickHouse реализованы:

- Row Policies;
- tenant-aware query execution.

---

## AC-4

Ни один tenant не может получить доступ к данным другого tenant.

Проверяется:

- автоматизированными тестами;
- integration tests;
- penetration tests.

---

## AC-5

При выполнении тяжёлых запросов одного tenant:

- не возникает критической деградации других tenant.

---

## AC-6

Создание нового tenant выполняется автоматически.

---

## AC-7

Миграции схемы выполняются централизованно.

---

## AC-8

Все backend-сервисы используют централизованный tenant-context.

---

## AC-9

Все security-sensitive операции логируются.

---

## AC-10

Подготовлен ADR/documentation package:

- выбранная архитектура;
- ограничения;
- риски;
- стратегия масштабирования;
- стратегия безопасности.

---

# Риски

| Риск | Вероятность | Влияние | Митигирующие меры |
|---|---|---|---|
| Межтенантная утечка | Высокая | Критичное | RLS + Policies + tests |
| Noisy Neighbor | Средняя | Высокое | quotas + workload isolation |
| Деградация ClickHouse | Средняя | Высокое | правильный PK/order strategy |
| Усложнение backend | Средняя | Среднее | centralized access layer |
| Рост стоимости инфраструктуры | Средняя | Высокое | shared architecture |
| Сложность миграций | Низкая | Среднее | unified schema management |

---

# Ожидаемый результат

В результате реализации должна быть получена:

- масштабируемая SaaS-архитектура;
- безопасная multi-tenant модель;
- экономически эффективная cloud-платформа;
- база для массового подключения SMB/Freemium клиентов;
- возможность дальнейшего расширения в Enterprise-сегмент.

---

# Рекомендуемое архитектурное решение (предварительное)

На текущем этапе рекомендуется:

## Основная архитектура

### Hybrid Shared Multi-Tenant Architecture

#### PostgreSQL
- усиленная tenant isolation;
- RLS;
- возможно schema-level separation.

#### ClickHouse
- shared large tables;
- tenant_id-based isolation.

#### Backend
- centralized tenant context;
- strict tenant-aware access.

---

## Enterprise Strategy

Для клиентов с повышенными требованиями к безопасности:

- рекомендовать On-Premise;
- либо рассматривать Dedicated Cloud как premium-опцию.

---

# Дополнительные артефакты (рекомендуется подготовить)

1. ADR по архитектуре multi-tenancy.
2. Threat Model.
3. Tenant Isolation Checklist.
4. Data Access Security Checklist.
5. Load Testing Plan.
6. Security Testing Plan.
7. Migration Strategy.
8. Rollback Strategy.
9. Observability Strategy.
10. Cost Model / TCO Analysis.

