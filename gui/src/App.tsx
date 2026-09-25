import { useEffect, useState } from 'react'
import { Navigate, Route, BrowserRouter, Routes } from 'react-router-dom'
import type { CurrentUser } from './api/client'
import { me } from './api/client'
import ChatEmpty from './features/chat/ChatEmpty'
import ChatLayout from './features/chat/ChatLayout'
import ChatView from './features/chat/ChatView'
import Dashboard from './pages/Dashboard'
import Login from './pages/Login'

function App() {
  const [user, setUser] = useState<CurrentUser | null>(null)
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    me()
      .then(setUser)
      .catch(() => setUser(null))
      .finally(() => setLoading(false))
  }, [])

  if (loading) {
    return null
  }

  return (
    <BrowserRouter>
      <Routes>
        <Route
          path="/login"
          element={user ? <Navigate to="/" replace /> : <Login onLoggedIn={setUser} />}
        />
        <Route
          path="/"
          element={
            user ? (
              <Dashboard user={user} onLoggedOut={() => setUser(null)} />
            ) : (
              <Navigate to="/login" replace />
            )
          }
        />
        <Route
          path="/sessions"
          element={
            user ? (
              <ChatLayout user={user} onLoggedOut={() => setUser(null)} />
            ) : (
              <Navigate to="/login" replace />
            )
          }
        >
          <Route index element={<ChatEmpty />} />
          <Route path=":sessionId" element={<ChatView />} />
        </Route>
      </Routes>
    </BrowserRouter>
  )
}

export default App
