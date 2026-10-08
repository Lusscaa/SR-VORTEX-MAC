import { useState, useEffect } from 'react'
import { AlertCircle, Trash2, RefreshCw } from 'lucide-react'

export default function SoftBlockedAccounts() {
  const [accounts, setAccounts] = useState([])
  const [loading, setLoading] = useState(true)
  const [selectedIds, setSelectedIds] = useState(new Set())
  const [deleting, setDeleting] = useState(false)

  useEffect(() => {
    loadAccounts()
  }, [])

  const loadAccounts = async () => {
    setLoading(true)
    try {
      const token = localStorage.getItem('panel_token')
      const res = await fetch('/api/accounts/soft-blocked', {
        headers: { Authorization: `Bearer ${token}` }
      })
      if (res.ok) {
        const data = await res.json()
        setAccounts(data)
      }
    } catch (e) {
      console.error('Erro ao carregar contas:', e)
    } finally {
      setLoading(false)
    }
  }

  const toggleSelect = (id) => {
    const newSelected = new Set(selectedIds)
    if (newSelected.has(id)) newSelected.delete(id)
    else newSelected.add(id)
    setSelectedIds(newSelected)
  }

  const deleteSelected = async () => {
    if (selectedIds.size === 0) return
    if (!window.confirm(`Deletar ${selectedIds.size} conta(s)?`)) return

    setDeleting(true)
    try {
      const token = localStorage.getItem('panel_token')
      const res = await fetch('/api/accounts/delete', {
        method: 'POST',
        headers: {
          'Authorization': `Bearer ${token}`,
          'Content-Type': 'application/json'
        },
        body: JSON.stringify({ values: Array.from(selectedIds) })
      })
      if (res.ok) {
        setAccounts(accounts.filter(a => !selectedIds.has(a.email)))
        setSelectedIds(new Set())
        alert('✅ Contas deletadas!')
      }
    } catch (e) {
      alert('❌ Erro ao deletar: ' + e.message)
    } finally {
      setDeleting(false)
    }
  }

  return (
    <div className="space-y-6">
      <div className="flex items-center gap-3">
        <AlertCircle className="w-6 h-6 text-orange-500" />
        <h1 className="text-2xl font-bold">Contas com Soft-block</h1>
      </div>

      <div className="bg-orange-50 dark:bg-orange-950/30 border border-orange-200 dark:border-orange-800 rounded-lg p-4">
        <p className="text-orange-800 dark:text-orange-200 text-sm">
          Essas contas deram soft-block (IP bloqueado temporariamente). Você pode deletá-las da lista ou tentar usar depois com outro IP/proxy.
        </p>
      </div>

      {loading ? (
        <div className="text-center py-8">
          <RefreshCw className="w-6 h-6 animate-spin mx-auto mb-2" />
          Carregando...
        </div>
      ) : accounts.length === 0 ? (
        <div className="text-center py-12 text-gray-500 dark:text-gray-400">
          Nenhuma conta com soft-block 🎉
        </div>
      ) : (
        <>
          <div className="flex gap-2 mb-4">
            <button
              onClick={() => setSelectedIds(new Set(accounts.map(a => a.email)))}
              className="text-sm px-3 py-1.5 bg-gray-200 dark:bg-gray-700 rounded hover:bg-gray-300 dark:hover:bg-gray-600"
            >
              Selecionar Todas
            </button>
            <button
              onClick={() => setSelectedIds(new Set())}
              className="text-sm px-3 py-1.5 bg-gray-200 dark:bg-gray-700 rounded hover:bg-gray-300 dark:hover:bg-gray-600"
            >
              Desselecionar
            </button>
            {selectedIds.size > 0 && (
              <button
                onClick={deleteSelected}
                disabled={deleting}
                className="text-sm px-3 py-1.5 bg-red-500 text-white rounded hover:bg-red-600 disabled:opacity-50 flex items-center gap-1"
              >
                <Trash2 className="w-4 h-4" />
                Deletar ({selectedIds.size})
              </button>
            )}
          </div>

          <div className="space-y-2 max-h-96 overflow-y-auto">
            {accounts.map(account => (
              <div
                key={account.email}
                className="flex items-center gap-3 p-3 bg-gray-100 dark:bg-gray-800 rounded-lg hover:bg-gray-200 dark:hover:bg-gray-700"
              >
                <input
                  type="checkbox"
                  checked={selectedIds.has(account.email)}
                  onChange={() => toggleSelect(account.email)}
                  className="w-4 h-4"
                />
                <div className="flex-1 min-w-0">
                  <p className="font-mono text-sm break-all">{account.email}</p>
                  <p className="text-xs text-gray-500 dark:text-gray-400">
                    {account.error_reason || 'Soft-block de IP'}
                  </p>
                </div>
              </div>
            ))}
          </div>
        </>
      )}
    </div>
  )
}
