import { Link, useNavigate } from 'react-router-dom'
import type { CurrentUser } from '../api/client'
import { logout } from '../api/client'

interface DashboardProps {
  user: CurrentUser
  onLoggedOut: () => void
}

export default function Dashboard({ user, onLoggedOut }: DashboardProps) {
  const navigate = useNavigate()

  async function handleLogout() {
    await logout()
    onLoggedOut()
    navigate('/login', { replace: true })
  }

  return (
    <main className="dashboard">
      <header>
        <h1>local-agent</h1>
        <button onClick={handleLogout}>Log out</button>
      </header>
      <p>
        Signed in as <strong>{user.email}</strong> ({user.role})
      </p>
      <p>
        <Link to="/sessions">Go to chat →</Link>
      </p>
    </main>
  )
}
