# Анализ форматирования дат в HsTable

## Где находится логика для таблицы

Формат даты из настроек компонента приходит в таблицу не напрямую как `config.dateFormat`, а через объект спецификации таблицы `spec`.

Цепочка начинается в генераторе спецификации:

- `src/adaptors/ReactabularChartAdaptor/ReactabularSpecGenerator.js`

В этом файле значение из конфигурации компонента переносится в спецификацию таблицы:

```js
spec.dateFormat = config.dateFormat;
```

После этого `HsTable` работает уже с `spec.dateFormat`.

Основная логика таблицы находится в:

- `src/components/HsTable/HsTable.js`

При построении колонок таблица вызывает метод `getCellFormatters`:

```js
const cellFormatters = this.getCellFormatters(column, props.data?.data, spec);
```

Метод расположен в `src/components/HsTable/HsTable.js` и отвечает за набор форматтеров ячейки. Среди прочих форматтеров он добавляет форматтер дат:

```js
addDateFormatCellFormatter({ cellFormatters, column, spec });
```

## Какой метод используется

Форматтер дат добавляется функцией:

- `src/components/HsTable/cellFormatters.js`
- `addDateFormatCellFormatter`

Код функции:

```js
export const addDateFormatCellFormatter = ({ cellFormatters, column, spec }) => {
  const dateFormat = column.dateFormat || spec.dateFormat;
  if (!(dateFormat === '-')) {
    cellFormatters.push((value) => {
      return dateFormatFunction(value, column.dateFormat || spec.dateFormat);
    });
  }
};
```

Эта функция определяет актуальный формат даты по приоритету:

```text
column.dateFormat -> spec.dateFormat
```

Где:

- `column.dateFormat` - формат даты на уровне конкретной колонки;
- `spec.dateFormat` - общий формат даты таблицы, полученный из `config.dateFormat`.

Если итоговый формат равен `'-'`, форматтер даты не добавляется.

## Где расположен метод форматирования даты

Непосредственное преобразование значения выполняет функция:

- `src/constants/formatOptions.js`
- `dateFormatFunction`

Она импортируется в `cellFormatters.js`:

```js
import { dateFormatFunction } from 'constants/formatOptions';
```

И вызывается так:

```js
dateFormatFunction(value, column.dateFormat || spec.dateFormat);
```

## Как работает логика форматирования

`dateFormatFunction` принимает два аргумента:

```js
dateFormatFunction(label, fmt)
```

Где:

- `label` - исходное значение ячейки;
- `fmt` - формат даты, например `DD.MM.YYYY`, `YYYY-MM-DD HH:mm`, `MMMM YYYY`.

Внутри функции выбирается шаблон форматирования:

```js
const template = fmt && fmt !== '-' ? fmt : 'DD.MM.YYYY';
```

То есть если формат не задан или равен `'-'`, используется дефолтный формат:

```text
DD.MM.YYYY
```

Дальше поведение зависит от типа значения:

1. Если значение пустое, функция возвращает пустую строку.

```js
if (!label) return '';
```

2. Если значение является объектом `Date`, оно форматируется через `moment`.

```js
ret = moment(label);
if (ret.isValid()) {
  ret = ret.format(template);
  return ret;
}
```

3. Если значение является строкой и в строке есть не только цифры, функция пытается распарсить ее как ISO 8601.

```js
ret = moment(label, [moment.ISO_8601], 'ru', true);
```

Если строка успешно распознана как дата, она форматируется по шаблону:

```js
if (ret.isValid()) ret = ret.format(template);
```

Если строка не распознана как дата, она возвращается без изменений:

```js
else ret = val;
```

4. Все остальные значения возвращаются как есть.

## Итоговая схема

```text
config.dateFormat
  -> ReactabularSpecGenerator.js
  -> spec.dateFormat
  -> HsTable.getColumns()
  -> HsTable.getCellFormatters()
  -> addDateFormatCellFormatter()
  -> dateFormatFunction()
  -> moment(...).format(...)
```

## Важные особенности

- `config.dateFormat` не используется напрямую внутри `HsTable.js`; туда он попадает как `spec.dateFormat`.
- Формат колонки `column.dateFormat` имеет приоритет над общим форматом таблицы `spec.dateFormat`.
- Значение `'-'` отключает добавление форматтера даты в `cellFormatters`.
- `addDateFormatCellFormatter` не проверяет `column.fieldType === 'date'`, поэтому форматтер может добавляться ко всем колонкам.
- Фактически изменяются только значения типа `Date` и строки, которые строго распознаются как ISO 8601.
- Обычные строки, которые не распознаны как даты, остаются без изменений.
