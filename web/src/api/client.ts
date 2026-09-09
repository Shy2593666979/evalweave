import axios from 'axios'

export const api = axios.create({ baseURL: '/api', withCredentials: true, timeout: 15_000 })

export function errorMessage(error: unknown): string {
  if (axios.isAxiosError(error)) return error.response?.data?.detail ?? error.message
  return error instanceof Error ? error.message : '操作失败'
}
