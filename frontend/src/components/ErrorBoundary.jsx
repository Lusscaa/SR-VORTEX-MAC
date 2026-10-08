import { Component } from 'react'

// Sem isto, um erro de render em UMA página derruba o app inteiro (tela em
// branco, sem sidebar). Aqui isolamos: a página quebrada mostra o erro e o
// resto do painel continua navegável.
export default class ErrorBoundary extends Component {
  constructor(props) {
    super(props)
    this.state = { error: null }
  }

  static getDerivedStateFromError(error) {
    return { error }
  }

  componentDidUpdate(prev) {
    // Ao trocar de rota, limpa o erro para tentar renderizar a nova página.
    if (prev.routeKey !== this.props.routeKey && this.state.error) {
      this.setState({ error: null })
    }
  }

  render() {
    if (this.state.error) {
      return (
        <div className="rounded-xl border border-red-300 dark:border-red-800 bg-red-50 dark:bg-red-950/30 p-4">
          <p className="font-semibold text-red-700 dark:text-red-300">Esta página falhou ao carregar.</p>
          <pre className="mt-2 whitespace-pre-wrap text-xs text-red-600 dark:text-red-400">
            {String(this.state.error?.message || this.state.error)}
          </pre>
          <button
            onClick={() => this.setState({ error: null })}
            className="mt-3 rounded-lg border border-red-300 dark:border-red-800 px-3 py-1.5 text-xs font-medium text-red-700 dark:text-red-300"
          >
            Tentar de novo
          </button>
        </div>
      )
    }
    return this.props.children
  }
}
