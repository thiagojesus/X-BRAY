import { BrowserRouter, Routes, Route } from 'react-router-dom'
import Layout from './components/Layout'
import Dashboard from './pages/Dashboard'
import Juros from './pages/Juros'
import Inflacao from './pages/Inflacao'
import IpcaDecomposicao from './pages/IpcaDecomposicao'
import Atividade from './pages/Atividade'
import Cambio from './pages/Cambio'
import Titulos from './pages/Titulos'
import Focus from './pages/Focus'
import Complementares from './pages/Complementares'
import TesouroDireto from './pages/TesouroDireto'

function App() {
  return (
    <BrowserRouter>
      <Routes>
        <Route path="/" element={<Layout />}>
          <Route index element={<Dashboard />} />
          <Route path="juros" element={<Juros />} />
          <Route path="inflacao" element={<Inflacao />} />
          <Route path="ipca-decomposicao" element={<IpcaDecomposicao />} />
          <Route path="atividade" element={<Atividade />} />
          <Route path="cambio" element={<Cambio />} />
          <Route path="titulos" element={<Titulos />} />
          <Route path="tesouro-direto" element={<TesouroDireto />} />
          <Route path="focus" element={<Focus />} />
          <Route path="complementares" element={<Complementares />} />
        </Route>
      </Routes>
    </BrowserRouter>
  )
}

export default App
