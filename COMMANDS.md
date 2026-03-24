# Команды админа

## /givepak — выдать пак игроку

Синтаксис: `/givepak <user_id> [тип]`

Типы: `weekly` (по умолч.), `special`, `russia`, `brazil`, `turkey`, `minirandom`, `morning`

- `weekly` — 5 карточек, обычный
- `special` — 5 карточек, повышенный шанс 80+
- `russia` — 1 русский игрок (1% Аршавин)
- `brazil` — 2 бразильца
- `turkey` — 1 турок
- `minirandom` — рандомно один из: russia / brazil / turkey
- `morning` — 2 карточки, ежедневный утренний пак (<70: 85%, 70-75: 10%, 76-80: 4.5%, 80+: 0.5%)

### Всем игрокам — обычный пак (weekly)
```
/givepak 308826725
/givepak 316877089
/givepak 560101647
```

### Всем игрокам — специальный пак (80+ чаще)
```
/givepak 308826725 special
/givepak 316877089 special
/givepak 560101647 special
```

### Всем игрокам — российский пак
```
/givepak 308826725 russia
/givepak 316877089 russia
/givepak 560101647 russia
```

### Всем игрокам — бразильский пак
```
/givepak 308826725 brazil
/givepak 316877089 brazil
/givepak 560101647 brazil
```

### Всем игрокам — турецкий пак
```
/givepak 308826725 turkey
/givepak 316877089 turkey
/givepak 560101647 turkey
```

### Всем игрокам — мини-рандом (russia/brazil/turkey)
```
/givepak 308826725 minirandom
/givepak 316877089 minirandom
/givepak 560101647 minirandom
```

### Всем игрокам — утренний пак
```
/givepak 308826725 morning
/givepak 316877089 morning
/givepak 560101647 morning
```

### Всем игрокам — саудовский пак 🇸🇦
```
/givepak 308826725 saudi
/givepak 316877089 saudi
/givepak 560101647 saudi
```

### Конкретному игроку
| Игрок     | user_id   |
|-----------|-----------|
| timurRRMa | 308826725 |
| Djavaded  | 316877089 |
| RRabb1dtt | 560101647 |
