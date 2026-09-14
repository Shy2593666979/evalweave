export type SystemRole = 'admin' | 'user'

export interface UserType {
  id: string
  code: string
  name: string
  description: string | null
  permissions: string[]
  selectable_on_registration: boolean
  is_active: boolean
}
export interface User {
  id: string
  username: string
  email: string | null
  system_role: SystemRole
  user_type_id: string | null
  user_type_name: string | null
  permissions: string[]
  is_active: boolean
  created_at: string
  last_login_at: string | null
}

export interface PermissionOption {
  key: string
  label: string
}
