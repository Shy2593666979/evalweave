const HAS_TIMEZONE = /(Z|[+-]\d{2}:?\d{2})$/i

export function formatBeijingDateTime(value: string | null, includeYear = false): string {
  if (!value) return '—'
  const instant = new Date(HAS_TIMEZONE.test(value) ? value : `${value}Z`)
  if (Number.isNaN(instant.getTime())) return value
  return new Intl.DateTimeFormat('zh-CN', {
    timeZone: 'Asia/Shanghai',
    ...(includeYear ? { year: 'numeric' as const } : {}),
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  }).format(instant)
}
