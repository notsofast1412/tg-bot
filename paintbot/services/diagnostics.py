from aiogram.exceptions import TelegramAPIError


def can_send(chat, member):
    status = member.status
    if status in ("creator", "administrator"):
        return True
    if status in ("left", "kicked"):
        return False
    if status == "restricted":
        return bool(member.is_member and member.can_send_messages)
    return bool(chat.permissions and chat.permissions.can_send_messages)


async def group_access(bot, group_id):
    me = await bot.get_me()
    chat = await bot.get_chat(group_id)
    if chat.type not in ("group", "supergroup"):
        raise ValueError("Нужна группа или супергруппа, а не канал/личный чат")
    member = await bot.get_chat_member(group_id, me.id)
    return can_send(chat, member), member.status


async def diagnose(bot, store, monitor):
    from paintbot.services.formatting import stamp
    from html import escape
    lines = []
    try:
        await bot.get_me()
        lines.append("Telegram API: getMe выполнен успешно")
    except TelegramAPIError as exc:
        lines.append("Telegram API: ошибка " + type(exc).__name__)
    group = store.setting("group_id")
    if group:
        try:
            allowed, status = await group_access(bot, int(group))
            lines.append(f"Группа {group}: getChat/getChatMember выполнены; статус {status}; право отправки: {'да' if allowed else 'нет'}")
        except (TelegramAPIError, ValueError) as exc:
            lines.append("Группа: проверка неуспешна — " + type(exc).__name__ + ". Проверьте ID, присутствие и права бота")
    else:
        lines.append("Группа не настроена: /setgroup")
    stats = store.statistics()
    lines.extend([f"Мониторинг: {'работает' if monitor.running else 'не запущен'}; последний цикл: {stamp(monitor.last_tick)}",
                  f"Ошибка цикла: {monitor.last_error or 'нет'}",
                  f"Базовый интервал: {monitor.interval} с; ошибки увеличивают интервал до 6 ч",
                  f"Последний успех: {stamp(stats['last_success'])}",
                  f"Последняя попытка: {stamp(stats['last_attempt'])}",
                  f"Активных: {stats['active']}; с ошибками: {stats['errors']}; очередь: {stats['queue']}"])
    lines.extend(f"{m}: {s}" for m, s in stats["markets"].items())
    lines.append("Статус площадки основан на последней попытке конкретного товара, а не на общей доступности. /queue — проблемные доставки")
    return escape("\n".join(lines))
