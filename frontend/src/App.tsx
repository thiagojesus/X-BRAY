import { BrowserRouter, Route, Routes } from 'react-router-dom'
import Layout from './components/Layout'
import Atividade from './pages/Atividade'
import Cambio from './pages/Cambio'
import Complementares from './pages/Complementares'
import Credito from './pages/Credito'
import Dashboard from './pages/Dashboard'
import Derivados from './pages/Derivados'
import Fiscal from './pages/Fiscal'
import Focus from './pages/Focus'
import Inflacao from './pages/Inflacao'
import IpcaDecomposicao from './pages/IpcaDecomposicao'
import Juros from './pages/Juros'
import { MacroScore } from './pages/MacroScore'
import SetorExterno from './pages/SetorExterno'
import TesouroDireto from './pages/TesouroDireto'
import Titulos from './pages/Titulos'

function App() {
  return (
    <BrowserRouter>
      <Routes>
        <Route path="/" element={<Layout />}>
          <Route index element={<Dashboard />} />
          <Route path="derivados" element={<Derivados />} />
          <Route path="macro-score" element={<MacroScore />} />
          <Route path="juros" element={<Juros />} />
          <Route path="inflacao" element={<Inflacao />} />
          <Route path="ipca-decomposicao" element={<IpcaDecomposicao />} />
          <Route path="atividade" element={<Atividade />} />
          <Route path="fiscal" element={<Fiscal />} />
          <Route path="credito" element={<Credito />} />
          <Route path="setor-externo" element={<SetorExterno />} />
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
