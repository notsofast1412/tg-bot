from aiogram.exceptions import TelegramAPIError


async def group_access(bot, group_id):
    lines = []
    try:
        me = await bot.get_me()
        lines.append("Telegram API: getMe выполнен успешно")
    except TelegramAPIError as e:
        return [f"Telegram API: ошибка {type(e).__name__}; группа не проверена"]
    if not group_id:
        return lines + ["Группа не настроена: /setgroup -100…"]
    try:
        chat = await bot.get_chat(int(group_id))
        if str(chat.type) not in ("group", "supergroup"):
            return lines + ["ID ведёт не в группу; настройте /setgroup"]
        lines.append(f"Группа {chat.id}: доступна")
        member = await bot.get_chat_member(chat.id, me.id)
        status = str(member.status)
        allowed = status in ("creator", "administrator")
        if status == "member":
            allowed = (
                chat.permissions is not None
                and chat.permissions.can_send_messages is True
            )
        if status == "restricted":
            allowed = member.is_member and member.can_send_messages
        lines.append(f"Участие бота: {status}")
        lines.append(
            "Права на отправку: подтверждены getChat/getChatMember"
            if allowed
            else "Права на отправку: не подтверждены; разрешите отправку или назначьте администратором"
        )
        lines.append(
            "Фактическую доставку проверяет отдельная кнопка тестовой отправки"
        )
    except TelegramAPIError as e:
        lines.append(
            f"Проверка группы: {type(e).__name__}; проверьте ID, добавьте бота и разрешите отправку"
        )
    return lines


async def test_group(bot, group_id):
    if not group_id:
        return "Группа не настроена. В личном чате: /setgroup -100…"
    try:
        # Это отдельная тестовая отправка, не событие цены и не элемент outbox.
        chat = await bot.get_chat(int(group_id))
        if str(chat.type) not in ("group", "supergroup"):
            return "Настроенный ID не является группой"
        result = await bot.send_message(
            int(group_id),
            "✅ PaintWatch: тестовая отправка. Это не уведомление об изменении цены.",
        )
        return f"Тест фактически отправлен в группу {result.chat.id}, сообщение #{result.message_id}"
    except TelegramAPIError as e:
        return f"Отправка не подтверждена: {type(e).__name__}. Проверьте ID, участие, права и сеть; при тайм-ауте проверьте группу перед повтором."
