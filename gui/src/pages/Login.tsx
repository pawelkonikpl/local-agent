import { type FormEvent, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import type { CurrentUser } from '../api/client'
import { ApiError, login } from '../api/client'

interface LoginProps {
  onLoggedIn: (user: CurrentUser) => void
}

export default function Login({ onLoggedIn }: LoginProps) {
  const navigate = useNavigate()
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [submitting, setSubmitting] = useState(false)

  async function handleSubmit(event: FormEvent) {
    event.preventDefault()
    setError(null)
    setSubmitting(true)
    try {
      const user = await login(email, password)
      onLoggedIn(user)
      navigate('/', { replace: true })
    } catch (err) {
      setError(err instanceof ApiError ? 'Invalid email or password.' : 'Login failed.')
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <main className="auth-screen">
      <form className="auth-form" onSubmit={handleSubmit}>
        <h1>local-agent</h1>
        <label htmlFor="email">Email</label>
        <input
          id="email"
          type="email"
          autoComplete="username"
          value={email}
          onChange={(e) => setEmail(e.target.value)}
          required
        />
        <label htmlFor="password">Password</label>
        <input
          id="password"
          type="password"
          autoComplete="current-password"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          required
        />
        {error && <p className="auth-error">{error}</p>}
        <button type="submit" disabled={submitting}>
          {submitting ? 'Signing in…' : 'Sign in'}
        </button>
      </form>
    </main>
  )
}
