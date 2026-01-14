import { useState, useEffect } from 'react'

function App() {
  const [health, setHealth] = useState<string>('checking...')

  useEffect(() => {
    // Simulate health check
    setHealth('healthy')
  }, [])

  return (
    <div className="min-h-screen bg-gray-100 flex items-center justify-center">
      <div className="bg-white p-8 rounded-lg shadow-md max-w-md w-full">
        <h1 className="text-2xl font-bold text-gray-800 mb-4">
          {{PROJECT_NAME}}
        </h1>
        <p className="text-gray-600 mb-4">
          Agent-generated React application
        </p>
        <div className="flex items-center gap-2">
          <span className={`w-2 h-2 rounded-full ${
            health === 'healthy' ? 'bg-green-500' : 'bg-yellow-500'
          }`} />
          <span className="text-sm text-gray-500">Status: {health}</span>
        </div>
      </div>
    </div>
  )
}

export default App
