import { useEffect, useState } from 'react'
import { Link, useNavigate, useSearchParams } from 'react-router-dom'
import { useAdminAuth } from './AuthProvider'

const reasonCopy: Record<string, string> = {
  access_denied: 'Esta conta não tem acesso administrativo.',
  not_authorized: 'Esta conta não tem acesso administrativo.',
  login_failed: 'Não foi possível concluir o acesso. Tente novamente.',
  session_expired: 'Sua sessão expirou. Entre novamente.',
  signed_out: 'Você saiu da área administrativa.',
}

export function beginGoogleLogin(assign: (url: string) => void = url => window.location.assign(url)) {
  assign('/api/auth/login')
}

export default function LoginPage({ navigateToLogin = beginGoogleLogin }: {
  navigateToLogin?: () => void
} = {}) {
  const { auth, refresh } = useAdminAuth()
  const [params] = useSearchParams()
  const navigate = useNavigate()
  const [pending, setPending] = useState(false)
  const message = reasonCopy[params.get('reason') ?? '']

  useEffect(() => {
    if (auth.status === 'authenticated') navigate('/admin', { replace: true })
  }, [auth.status, navigate])

  useEffect(() => {
    const onPageShow = () => setPending(false)
    window.addEventListener('pageshow', onPageShow)
    return () => window.removeEventListener('pageshow', onPageShow)
  }, [])

  const startLogin = () => {
    if (pending || auth.status !== 'unauthenticated') return
    setPending(true)
    navigateToLogin()
  }

  if (auth.status === 'loading') {
    return <main className="admin-auth-page" aria-busy="true"><p role="status">Verificando acesso…</p></main>
  }

  if (auth.status === 'forbidden') {
    return <main className="admin-auth-page"><section className="admin-auth-card" role="alert">
      <h1>Acesso administrativo</h1><p>Esta conta não tem acesso administrativo.</p>
      <Link to="/">Voltar ao site</Link>
    </section></main>
  }

  return <main className="admin-auth-page">
    <section className="admin-auth-card" aria-labelledby="admin-login-title">
      <p className="admin-eyebrow">X-BRAY · ACESSO RESTRITO</p>
      <h1 id="admin-login-title">Área administrativa</h1>
      <p>Entre com uma conta Google autorizada pela equipe.</p>
      {message && <p className="admin-auth-message" role="status">{message}</p>}
      {auth.status === 'unavailable' ? <div role="alert" className="admin-auth-message">
        <p>O serviço de autenticação está indisponível. Tente novamente em instantes.</p>
        <button type="button" className="admin-button" onClick={() => void refresh(true)}>Tentar novamente</button>
      </div> : <button
        type="button" className="admin-button admin-google-button"
        onClick={startLogin} disabled={pending || auth.status !== 'unauthenticated'}
      >{pending ? 'Abrindo acesso Google…' : 'Entrar com Google'}</button>}
      <Link className="admin-back-link" to="/">Voltar ao site público</Link>
    </section>
  </main>
}
