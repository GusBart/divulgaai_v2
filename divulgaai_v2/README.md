# Promo Builder Telegram v5.3

Login inicial:
- usuário: admin
- senha: 1234

## Mudanças desta versão
- Subadmin não pode editar a senha de administrador nem de subadmin.
- Imagem agora aparece em `Posts para revisão / agendados`.
- Bloqueio de palavras-chave para apostas, conteúdo adulto e palavrões.
- Removido `Texto sobre a imagem` do editor.
- Cards de dashboard (`Usuários ativos`, `Posts enviados`, `Aguardando revisão`, `Rascunhos`, `Agendados`, `Recusados`) aparecem apenas para admin e subadmin.

## Rodar local
```bash
pip install -r requirements.txt
python app.py
```

## Deploy no Render
Build:
```bash
pip install -r requirements.txt
```

Start:
```bash
python app.py
```

Variáveis:
- BOT_TOKEN
- TELEGRAM_CHAT_ID
- APP_SECRET_KEY
