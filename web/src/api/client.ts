import axios from 'axios'

export const api = axios.create({ baseURL: '/api', withCredentials: true, timeout: 15_000 })

api.interceptors.response.use((response) => {
  const body = response.data
  if (
    body &&
    typeof body === 'object' &&
    !Array.isArray(body) &&
    'code' in body &&
    'message' in body &&
    'data' in body
  ) {
    response.data = body.data
  }
  return response
})

export function errorMessage(error: unknown): string {
  if (axios.isAxiosError(error)) {
    return error.response?.data?.message ?? error.response?.data?.detail ?? error.message
  }
  return error instanceof Error ? error.message : '操作失败'
}
