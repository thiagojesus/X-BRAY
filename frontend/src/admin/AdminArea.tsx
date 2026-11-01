import { useEffect, useMemo, useState } from 'react'
import { Link, Navigate, useLocation, useNavigate } from 'react-router-dom'
import { useAdminAuth } from './AuthProvider'
import { coverageSchema, overviewSchema, providersSchema, runDetailSchema, runsSchema, type Coverage, type Overview, type Providers, type RunDetail, type RunsPage, type TaskCounts as TaskCountsDto } from './adminApi'
import { usePrivateResource } from './usePrivateResource'

const providerNames: Record<string, string> = { bcb_sgs: 'BCB SGS', focus: 'BCB Focus', tesouro: 'Tesouro Direto', anbima: 'ANBIMA', b3: 'B3', ibge_sidra: 'IBGE SIDRA' }
const stateNames: Record<string, string> = { running: 'Em execução', succeeded: 'Concluída', partial: 'Parcial', failed: 'Falhou', pending: 'Pendente', claimed: 'Em processamento', skipped: 'Ignorada', metadata_unknown: 'Metadados desconhecidos' }
const categoryNames: Record<string, string> = { network_timeout: 'Tempo limite de rede', network_reset: 'Conexão interrompida', temporary_server_error: 'Erro temporário do servidor', rate_limited: 'Limite de requisições', envelope_error: 'Resposta inválida', invalid_schema: 'Formato incompatível', semantic_conflict: 'Conflito semântico', incomplete_coverage: 'Cobertura incompleta', persistence_failure: 'Falha ao persistir', freshness_failure: 'Falha de atualização', logging_failure: 'Falha de registro', configuration_error: 'Configuração', authentication_error: 'Autenticação da fonte', forbidden_host: 'Host não permitido', internal_error: 'Erro interno', deferred: 'Adiada', circuit_open: 'Circuito aberto' }
const deferralNames: Record<string, string> = { publisher_cooldown: 'Aguardar intervalo do publicador', source_dependency_deferred: 'Aguardando fonte dependente' }
const errorCodeNames: Record<string, string> = { ReadTimeout: 'Tempo limite de leitura', ConnectTimeout: 'Tempo limite de conexão', WriteTimeout: 'Tempo limite de envio', PoolTimeout: 'Limite de conexões', TimeoutError: 'Tempo limite', HTTPStatusError: 'Resposta HTTP de erro', InvalidSGSPayloadError: 'Dados SGS inválidos', ProviderInvalidJSONResponseError: 'Resposta JSON inválida', ProviderResponseDecodingError: 'Falha ao decodificar resposta', NoSuccessfulSourceTasks: 'Nenhuma tarefa de fonte concluída', retired_source: 'Fonte desativada', invalid_sgs_payload: 'Dados SGS inválidos', invalid_json: 'JSON inválido', content_decoding_error: 'Falha ao decodificar conteúdo' }
const circuitNames: Record<string, string> = { closed: 'Fechado no registro', open: 'Aberto no registro', half_open: 'Meio aberto no registro', not_recorded: 'Não registrado', metadata_unknown: 'Metadados desconhecidos' }
const admissionNames: Record<string, string> = { next_admission_reassessment_due: 'Reavaliação de admissão devida', temporarily_deferred: 'Admissão temporariamente adiada', probe_lease_active: 'Concessão de sondagem registrada', not_deferred: 'Sem adiamento registrado', metadata_unknown: 'Metadados desconhecidos' }
const providers = Object.keys(providerNames)

function time(value: string | null | undefined): string {
  if (!value) return 'Não registrado'
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? 'Metadados desconhecidos' : `${new Intl.DateTimeFormat('pt-BR', { dateStyle: 'short', timeStyle: 'medium', timeZone: 'America/Sao_Paulo' }).format(date)} BRT`
}

function unknown(value: unknown): string {
  return typeof value === 'string' && value ? value : 'Metadados desconhecidos'
}

function safeWorkflowUrl(value: string | null): string | null {
  if (!value) return null
  try {
    const url = new URL(value)
    return url.origin === 'https://github.com' && /^\/thiagojesus\/X-BRAY\/actions\/runs\/\d+$/.test(url.pathname) ? url.href : null
  } catch { return null }
}

function decodeRunId(value: string): string {
  try { return decodeURIComponent(value) } catch { return '' }
}

function ResourceState<T>({ resource, children, onRetry }: { resource: { data: T | null; error: 'invalid' | 'unavailable' | 'notfound' | null; loading: boolean }; children: (data: T) => React.ReactNode; onRetry: () => void }) {
  if (resource.error) return <section className="admin-panel-card" role="alert"><h2>{resource.error === 'notfound' ? 'Execução não registrada' : 'Leitura indisponível'}</h2><p>{resource.error === 'invalid' ? 'A resposta não corresponde ao contrato esperado ou os filtros/cursor foram recusados. Esta seção foi ocultada.' : resource.error === 'notfound' ? 'Não há registro autorizado para este identificador.' : 'Não foi possível consultar os dados agora. Nenhum dado antigo será apresentado como atual.'}</p><button type="button" className="admin-button" onClick={onRetry}>Tentar novamente</button></section>
  if (resource.loading && !resource.data) return <p className="admin-state" role="status" aria-busy="true">Consultando dados registrados…</p>
  return resource.data ? <>{resource.loading && <p className="admin-state" role="status">Atualizando os dados registrados…</p>}{children(resource.data)}</> : null
}

function OverviewPage() {
  const overview = usePrivateResource('/api/admin/overview', overviewSchema)
  const sources = usePrivateResource('/api/admin/providers', providersSchema)
  return <section aria-labelledby="admin-overview-title">
    <header className="admin-page-heading"><div><p className="admin-eyebrow">VISÃO GERAL</p><h2 id="admin-overview-title">Estado registrado</h2><p>Snapshot de leitura; não inicia nem atualiza fontes.</p></div><button type="button" className="admin-button" onClick={() => { overview.reload(); sources.reload() }}>Reconsultar painel</button></header>
    <ResourceState resource={overview} onRetry={overview.reload}>{data => <OverviewCards data={data} />}</ResourceState>
    <ResourceState resource={sources} onRetry={sources.reload}>{data => <ProviderCards data={data} />}</ResourceState>
  </section>
}

function OverviewCards({ data }: { data: Overview }) {
  return <>
    {data.state === 'no_backend_run_record' && <p className="admin-notice" role="status">Sem registro de execução no backend. Uma falha do GitHub Actions antes do registro pode não aparecer aqui.</p>}
    <p className="admin-snapshot">Gerado {time(data.generated_at)} · referência {time(data.as_of)} · escopo: {data.scope}</p>
    <div className="admin-card-grid"><article className="admin-panel-card"><h3>Última tentativa registrada</h3><RunSummary value={data.latest_attempt} /></article><article className="admin-panel-card"><h3>Última execução totalmente concluída</h3><RunSummary value={data.latest_fully_successful} /></article><article className="admin-panel-card"><h3>Execuções ativas</h3><strong>{data.active_runs.count}</strong><p>Contagem pelo estado persistido: {data.active_runs.scope}</p></article></div>
    {data.latest_attempt && <TaskCounts counts={data.latest_attempt.task_counts} />}
    <section className="admin-panel-card"><h3>Últimas capturas validadas</h3><dl className="admin-facts">{providers.map(provider => <div key={provider}><dt>{providerNames[provider]}</dt><dd>{time(data.latest_valid_captures[provider])}</dd></div>)}</dl><p>{data.provider_capture_scope}</p></section>
  </>
}

function RunSummary({ value }: { value: Overview['latest_attempt'] }) {
  if (!value) return <p>Não registrado</p>
  const workflow = safeWorkflowUrl(value.workflow_url)
  return <dl className="admin-facts"><div><dt>Estado</dt><dd>{stateNames[value.state] ?? 'Metadados desconhecidos'}</dd></div><div><dt>Identificador</dt><dd><Link to={`/admin/atualizacoes/${encodeURIComponent(value.run_id)}`}>{value.run_id}</Link></dd></div><div><dt>Início</dt><dd>{time(value.created_at)}</dd></div><div><dt>Fim</dt><dd>{time(value.finished_at)}</dd></div><div><dt>Manifesto registrado</dt><dd>{value.manifest_version}</dd></div>{workflow && <div><dt>Execução no GitHub</dt><dd><a href={workflow} target="_blank" rel="noopener noreferrer">Ver no GitHub</a></dd></div>}</dl>
}

function TaskCounts({ counts }: { counts: TaskCountsDto }) {
  return <section className="admin-panel-card"><h3>Contagens da última tentativa</h3><div className="admin-table-wrap"><table><thead><tr><th>Tarefa</th><th>Estado registrado</th><th>Quantidade</th></tr></thead><tbody>{Object.entries(counts).flatMap(([task, statuses]) => Object.entries(statuses).map(([status, count]) => <tr key={`${task}:${status}`}><td>{task}</td><td>{stateNames[status] ?? 'Metadados desconhecidos'}</td><td>{count}</td></tr>))}</tbody></table></div></section>
}

function ProviderCards({ data }: { data: Providers }) {
  return <section className="admin-panel-card"><header className="admin-section-heading"><h3>Fontes e estado registrado</h3><Link to="/admin/cobertura">Consultar cobertura</Link></header><div className="admin-table-wrap"><table><thead><tr><th>Fonte</th><th>Circuito registrado</th><th>Admissão</th><th>Reavaliação em</th><th>Última captura válida</th><th>Falha de aquisição</th></tr></thead><tbody>{data.items.map(item => <tr key={item.provider}><td>{providerNames[item.provider]}</td><td>{item.circuit.recorded_state === 'open' && item.circuit.admission_view === 'next_admission_reassessment_due' ? 'Aberto no registro; reavaliação de admissão devida' : circuitNames[item.circuit.recorded_state] ?? 'Metadados desconhecidos'}</td><td>{admissionNames[item.circuit.admission_view] ?? 'Metadados desconhecidos'}</td><td>{time(item.circuit.opened_until ?? item.circuit.retry_not_before ?? item.circuit.probe_lease_until)}</td><td>{time(item.last_valid_capture_at)}</td><td>{item.latest_acquisition_failure_category ? categoryNames[item.latest_acquisition_failure_category] ?? 'Categoria desconhecida' : 'Não registrado'}</td></tr>)}</tbody></table></div><p>Atualizado {time(data.as_of)} · {data.scope}</p></section>
}

function localDate(value: Date): string { return new Date(value.getTime() - value.getTimezoneOffset() * 60_000).toISOString().slice(0, 10) }
function initialBounds(): { since: string; until: string } { const today = new Date(); const start = new Date(today); start.setDate(start.getDate() - 29); return { since: localDate(start), until: localDate(today) } }
function brDateStart(value: string): string { return `${value}T00:00:00-03:00` }
function brDateEndExclusive(value: string): string { const [year, month, day] = value.split('-').map(Number); const next = new Date(Date.UTC(year, month - 1, day + 1)); return `${next.toISOString().slice(0, 10)}T00:00:00-03:00` }

function HistoryPage() {
  const location = useLocation()
  const navigate = useNavigate()
  const params = new URLSearchParams(location.search)
  const defaults = useMemo(initialBounds, [])
  const [since, setSince] = useState(() => params.get('since') ?? defaults.since)
  const [until, setUntil] = useState(() => params.get('until') ?? defaults.until)
  const [filterState, setFilterState] = useState(params.get('state') ?? '')
  const cursor = params.get('cursor')
  const [cursorStack, setCursorStack] = useState<(string | null)[]>([])
  const query = new URLSearchParams({ limit: '50', since: brDateStart(since), until: brDateEndExclusive(until) })
  if (filterState) query.set('state', filterState)
  if (cursor) query.set('cursor', cursor)
  const resource = usePrivateResource(`/api/admin/runs?${query.toString()}`, runsSchema)

  useEffect(() => {
    const current = new URLSearchParams(location.search)
    setSince(current.get('since') ?? defaults.since)
    setUntil(current.get('until') ?? defaults.until)
    setFilterState(current.get('state') ?? '')
  }, [defaults.since, defaults.until, location.search])

  const apply = (event: React.FormEvent) => {
    event.preventDefault()
    setCursorStack([])
    const next = new URLSearchParams()
    next.set('since', since); next.set('until', until)
    if (filterState) next.set('state', filterState)
    navigate(`/admin/atualizacoes?${next}`)
  }
  const nextPage = () => {
    if (!resource.data?.next_cursor) return
    setCursorStack(stack => [...stack, cursor])
    const nextCursor = resource.data.next_cursor
    if (!nextCursor) return
    const next = new URLSearchParams(location.search); next.set('cursor', nextCursor)
    navigate(`/admin/atualizacoes?${next}`)
  }
  const previousPage = () => {
    const stack = [...cursorStack]
    const prior = stack.pop()
    setCursorStack(stack)
    const next = new URLSearchParams(location.search)
    if (prior) next.set('cursor', prior); else next.delete('cursor')
    navigate(`/admin/atualizacoes?${next}`)
  }
  const resetCursor = () => { setCursorStack([]); const next = new URLSearchParams(location.search); next.delete('cursor'); navigate(`/admin/atualizacoes?${next}`) }
  return <section><header className="admin-page-heading"><div><p className="admin-eyebrow">HISTÓRICO</p><h2>Execuções registradas</h2></div><button type="button" className="admin-button" onClick={resource.reload}>Reconsultar</button></header>
    <form className="admin-filters" onSubmit={apply}><label>Desde (data local)<input type="date" value={since} onChange={event => setSince(event.target.value)} required /></label><label>Até (inclusive)<input type="date" value={until} onChange={event => setUntil(event.target.value)} required /></label><label>Estado<select value={filterState} onChange={event => setFilterState(event.target.value)}><option value="">Todos</option>{['running', 'succeeded', 'partial', 'failed'].map(state => <option key={state} value={state}>{stateNames[state]}</option>)}</select></label><button type="submit" className="admin-button">Aplicar filtros</button></form>
    <p className="admin-help">As datas são interpretadas no horário de Brasília; o limite final exclusivo inclui todo o dia escolhido.</p>
    <ResourceState resource={resource} onRetry={resource.reload}>{page => <RunsTable page={page} />}</ResourceState>
    {resource.error === 'invalid' && <p className="admin-notice" role="alert">Cursor ou filtros recusados pelo servidor (422). Volte à primeira página e reaplique os filtros.</p>}
    <div className="admin-pagination"><button type="button" className="admin-button" onClick={previousPage} disabled={cursorStack.length === 0}>Página anterior</button><span>{cursor ? 'Página seguinte do período selecionado' : 'Primeira página'}</span><button type="button" className="admin-button" onClick={nextPage} disabled={!resource.data?.has_more}>Próxima página</button>{cursor && <button type="button" className="admin-link-button" onClick={resetCursor}>Reiniciar paginação</button>}</div>
  </section>
}

function WorkflowLink({ value }: { value: string | null }) {
  const href = safeWorkflowUrl(value)
  return href ? <> · <a href={href} target="_blank" rel="noopener noreferrer">GitHub</a></> : null
}

function RunsTable({ page }: { page: RunsPage }) {
  return <><p className="admin-snapshot">Gerado {time(page.generated_at)} · as of {time(page.as_of)} · {page.scope}</p><div className="admin-table-wrap"><table><caption>Execuções registradas nesta página</caption><thead><tr><th>Execução</th><th>Estado</th><th>Início</th><th>Fim</th><th>Duração</th><th>Manifesto</th><th>Contagens registradas</th></tr></thead><tbody>{page.items.map(item => <tr key={`${item.created_at}:${item.run_id}`}><td><Link to={`/admin/atualizacoes/${encodeURIComponent(item.run_id)}`}>{item.run_id}</Link>{safeWorkflowUrl(item.workflow_url) && <WorkflowLink value={item.workflow_url} />}</td><td>{stateNames[item.state] ?? 'Metadados desconhecidos'}</td><td>{time(item.created_at)}</td><td>{time(item.finished_at)}</td><td>{item.duration_seconds === null ? 'Não registrado' : `${item.duration_seconds.toLocaleString('pt-BR')} s`}</td><td>{item.manifest_version}</td><td>{Object.entries(item.task_counts).map(([task, states]) => `${task}: ${Object.entries(states).map(([state, count]) => `${stateNames[state] ?? state} ${count}`).join(', ')}`).join(' · ') || 'Não registrado'}</td></tr>)}</tbody></table></div>{page.items.length === 0 && <p role="status">Nenhuma execução registrada neste período.</p>}</>
}

function DetailPage({ runId }: { runId: string }) {
  const location = useLocation()
  const navigate = useNavigate()
  const safe = /^[A-Za-z0-9_.:-]{1,128}$/.test(runId)
  const taskCursor = new URLSearchParams(location.search).get('task_cursor')
  const resource = usePrivateResource(`/api/admin/runs/${encodeURIComponent(runId)}?task_limit=50${taskCursor ? `&task_cursor=${encodeURIComponent(taskCursor)}` : ''}`, runDetailSchema)
  if (!safe) return <p className="admin-notice" role="alert">Identificador de execução inválido.</p>
  return <ResourceState resource={resource} onRetry={resource.reload}>{data => <><RunDetailView data={data} /><div className="admin-pagination"><button type="button" className="admin-button" disabled={!taskCursor} onClick={() => navigate(`/admin/atualizacoes/${encodeURIComponent(runId)}`)}>Primeira página</button><span>{taskCursor ? `Tarefas após ${taskCursor}` : 'Primeira página de tarefas'}</span><button type="button" className="admin-button" disabled={!data.task_page.has_more || data.task_page.next_cursor === null} onClick={() => navigate(`/admin/atualizacoes/${encodeURIComponent(runId)}?task_cursor=${data.task_page.next_cursor}`)}>Próximas tarefas</button></div></>}</ResourceState>
}

function RunDetailView({ data }: { data: RunDetail }) {
  const summary = data.run.recorded_final_summary
  return <section><header className="admin-page-heading"><div><p className="admin-eyebrow">DETALHE DA EXECUÇÃO</p><h2>{data.run.run_id}</h2></div><button type="button" className="admin-button" onClick={() => window.location.reload()}>Reconsultar execução</button></header><p className="admin-snapshot">Gerado {time(data.generated_at)} · {data.scope}</p>
    <section className="admin-panel-card"><h3>Resultado registrado nesta execução</h3><dl className="admin-facts"><div><dt>Estado</dt><dd>{stateNames[data.run.state] ?? 'Metadados desconhecidos'}</dd></div><div><dt>Início</dt><dd>{time(data.run.created_at)}</dd></div><div><dt>Fim</dt><dd>{time(data.run.finished_at)}</dd></div><div><dt>Manifesto</dt><dd>{data.run.manifest_version}</dd></div><div><dt>Resumo final</dt><dd>{summary.status}{summary.task_count === null ? '' : ` · ${summary.task_count} tarefas`}</dd></div><div><dt>Plano SGS</dt><dd>{data.run.sgs_plan.version} · {data.run.sgs_plan.sha256 ?? 'não registrado'}</dd></div><div><dt>Diagnósticos</dt><dd>{data.diagnostics.status === 'not_recorded' ? 'Não registrado' : unknown(data.diagnostics.status)}</dd></div></dl><p className="admin-notice">A frescura atual não está incluída neste retrato histórico. {data.current_freshness.reason}</p></section>
    <section className="admin-panel-card"><h3>Tarefas registradas ({data.task_page.total})</h3><div className="admin-table-wrap"><table><thead><tr><th>Tarefa</th><th>Estado</th><th>Tentativas registradas</th><th>Registros</th><th>Séries atualizadas</th><th>Duração</th><th>Próxima tentativa</th><th>Motivo / categoria</th></tr></thead><tbody>{data.task_page.items.map(task => <tr key={task.task_order}><td>{task.task}</td><td>{stateNames[task.status] ?? 'Metadados desconhecidos'}</td><td>{task.attempt_count ?? 'Não registrado'}</td><td>{task.record_count ?? 'Não registrado'}</td><td>{task.series_updated ?? 'Não registrado'}</td><td>{task.duration_seconds === null ? 'Não registrado' : `${task.duration_seconds.toLocaleString('pt-BR')} s`}</td><td>{time(task.next_attempt_at)}</td><td>{deferralNames[task.deferral_reason ?? ''] ?? categoryNames[task.last_error_category ?? ''] ?? errorCodeNames[task.error_code ?? ''] ?? (task.error_code ? 'Código de erro conhecido' : 'Não registrado')}</td></tr>)}</tbody></table></div>{data.task_page.has_more && <p className="admin-notice">Lista limitada a {data.task_page.limit} tarefas; outras páginas estão disponíveis pela API.</p>}</section>
  </section>
}

function CoveragePage() {
  const location = useLocation()
  const navigate = useNavigate()
  const selected = new URLSearchParams(location.search).get('provider') ?? 'bcb_sgs'
  const validProvider = providers.includes(selected) ? selected : 'bcb_sgs'
  const cursor = new URLSearchParams(location.search).get('cursor') ?? '0'
  const sources = usePrivateResource('/api/admin/providers', providersSchema)
  const coverage = usePrivateResource(`/api/admin/coverage?provider=${validProvider}&limit=50&cursor=${encodeURIComponent(cursor)}`, coverageSchema)
  return <section><header className="admin-page-heading"><div><p className="admin-eyebrow">COBERTURA</p><h2>Cobertura e registros das fontes</h2><p>Disponibilidade documentada não representa captura; histórico não implica ausência de observações.</p></div><button type="button" className="admin-button" onClick={() => { sources.reload(); coverage.reload() }}>Reconsultar</button></header>
    <label className="admin-provider-select">Fonte<select value={validProvider} onChange={event => navigate(`/admin/cobertura?provider=${event.target.value}`)}>{providers.map(provider => <option key={provider} value={provider}>{providerNames[provider]}</option>)}</select></label>
    <ResourceState resource={sources} onRetry={sources.reload}>{data => <ProviderCards data={data} />}</ResourceState>
    <ResourceState resource={coverage} onRetry={coverage.reload}>{data => <><CoverageResult data={data} /><div className="admin-pagination"><button type="button" className="admin-button" disabled={cursor === '0'} onClick={() => navigate(`/admin/cobertura?provider=${validProvider}&cursor=0`)}>Primeira página</button><span>Posição {cursor}</span><button type="button" className="admin-button" disabled={!data.has_more || data.next_cursor === null} onClick={() => navigate(`/admin/cobertura?provider=${validProvider}&cursor=${data.next_cursor}`)}>Próxima página</button></div></>}</ResourceState>
  </section>
}

function CoverageResult({ data }: { data: Coverage }) {
  if (data.provider !== 'bcb_sgs' || data.coverage_status === 'metadata_unknown') return <section className="admin-panel-card"><h3>{providerNames[data.provider]}</h3><p className="admin-notice">Cobertura ainda não avaliada: metadados/contrato insuficientes.</p><p>Estado: {data.coverage_status} · motivo: {data.reason ?? 'metadados desconhecidos'} · itens: {data.items_status ?? 'não registrado'}.</p>{data.latest_valid_capture_at !== undefined && <p>Última captura válida: {time(data.latest_valid_capture_at)}</p>}{data.latest_reference !== undefined && <p>Referência registrada: {data.latest_reference ?? 'Não registrado'} · capturada em {time(data.latest_reference_captured_at)}</p>}</section>
  return <><p className="admin-snapshot">Escopo: {data.scope} · gerado {time(data.generated_at)} · as of {time(data.as_of)}</p><section className="admin-panel-card"><h3>Séries e evidências SGS</h3><p>Intervalos comprovam somente os recibos listados; não inferimos lacunas de calendário. “Antes do início documentado” é excluído, não é uma lacuna.</p><div className="admin-table-wrap"><table><thead><tr><th>Série</th><th>Frequência / unidade</th><th>Referência / captura</th><th>Freshness</th><th>Inícios</th><th>Situação e limites</th><th>Intervalos de prova</th></tr></thead><tbody>{data.items.map(item => <tr key={item.series_code}><td>{item.name}<br /><small>{item.series_code} · {item.series_key}</small></td><td>{item.frequency} · {item.unit}</td><td>{item.latest_reference ?? 'Não registrado'}<br />{time(item.last_valid_capture_at)}</td><td>{item.freshness_days} dias</td><td>Configurado: {item.configured_start ?? 'Não registrado'}<br />Documentado: {item.documented_start ?? 'Não registrado'}<br />Primeiro período: {item.first_reference_period ?? 'Não registrado'}</td><td><StatusFlags flags={item.status_flags} />{item.proof_search.status === 'metadata_unknown' || item.capture_search.status === 'metadata_unknown' ? <strong>Verificação limitada: cobertura não determinada.</strong> : item.coverage_status === 'metadata_unknown' && <strong>Metadados insuficientes para determinar cobertura.</strong>}<p>{item.capture_search.candidates_examined}/{item.capture_search.candidate_budget} candidatos de captura; prova {item.proof_search.candidates_examined}/{item.proof_search.candidate_budget}.</p>{item.proof_intervals_truncated && <p>Intervalos truncados após o limite de exibição.</p>}</td><td><details><summary>{item.acquired_proof_intervals.length} intervalos comprovados</summary>{item.acquired_proof_intervals.map(interval => <p key={`${interval.requested_start}:${interval.requested_end}:${interval.reference_start}:${interval.reference_end}:${interval.captured_at}`}>{interval.requested_start ?? '?'} a {interval.requested_end ?? '?'} · referência {interval.reference_start ?? '?'}–{interval.reference_end ?? '?'} · captura {time(interval.captured_at)}</p>)}</details></td></tr>)}</tbody></table></div><p>{data.has_more ? `Lista paginada; próxima posição ${data.next_cursor ?? 'não registrada'}.` : 'Fim da página atual.'}</p></section></>
}

function StatusFlags({ flags }: { flags: string[] }) {
  const labels: Record<string, string> = { acquisition_unverified: 'Aquisição não verificada', history_pending: 'Histórico pendente (busca completa)', stale_reference: 'Referência antiga', before_documented_start: 'Antes do início documentado (excluído)', metadata_unknown: 'Metadados desconhecidos' }
  return <ul className="admin-status-flags">{flags.map(flag => <li key={flag}>{labels[flag] ?? 'Estado desconhecido'}</li>)}</ul>
}

export function AdminGate() {
  const { auth, logout, logoutError, retryLogout, refresh, privateEpoch } = useAdminAuth()
  const navigate = useNavigate()
  const location = useLocation()
  useEffect(() => {
    if (auth.status === 'unauthenticated') navigate('/login?reason=session_expired', { replace: true })
    if (auth.status === 'forbidden') navigate('/login?reason=access_denied', { replace: true })
  }, [auth.status, navigate])
  if (auth.status === 'loading') return <main className="admin-auth-page" aria-busy="true"><p role="status">Verificando acesso…</p></main>
  if (auth.status === 'unauthenticated' || auth.status === 'forbidden') return null
  if (auth.status === 'unavailable' || !auth.session) return <main className="admin-auth-page"><section className="admin-auth-card" role={logoutError ? 'alert' : 'status'}><h1>{logoutError ? 'Saída não confirmada' : 'Área administrativa indisponível'}</h1><p>{logoutError ? 'Não foi possível confirmar sua saída. A sessão do servidor pode continuar ativa.' : 'Não foi possível verificar sua sessão. Os dados privados permanecem ocultos.'}</p>{logoutError ? <button type="button" className="admin-button" onClick={() => void retryLogout()}>Tentar sair novamente</button> : <button type="button" className="admin-button" onClick={() => void refresh(true)}>Tentar novamente</button>}<Link className="admin-back-link" to="/">Voltar ao site público</Link></section></main>

  const path = location.pathname.replace(/\/$/, '') || '/admin'
  const detail = path.match(/^\/admin\/atualizacoes\/([^/]+)$/)
  let content: React.ReactNode
  if (path === '/admin' || path === '/admin/visao-geral') content = <OverviewPage />
  else if (path === '/admin/atualizacoes') content = <HistoryPage />
  else if (detail) content = <DetailPage key={`${privateEpoch}:${detail[1]}`} runId={decodeRunId(detail[1])} />
  else if (path === '/admin/cobertura') content = <CoveragePage />
  else content = <Navigate to="/admin" replace />

  return <main className="admin-private" id="admin-private-content" key={privateEpoch}>
    <header className="admin-header"><div><p className="admin-eyebrow">X-BRAY · ACESSO RESTRITO</p><h1>Área administrativa</h1></div><div className="admin-actions"><Link className="admin-back-link" to="/">Voltar ao site</Link><button type="button" className="admin-button admin-logout" onClick={() => void logout()}>Sair</button></div></header>
    {logoutError && <p className="admin-auth-message" role="alert">Saída não confirmada. A sessão do servidor pode continuar ativa.<button type="button" className="admin-button" onClick={() => void retryLogout()}>Tentar sair novamente</button></p>}
    <nav className="admin-nav" aria-label="Navegação administrativa"><Link to="/admin">Visão geral</Link><Link to="/admin/atualizacoes">Atualizações</Link><Link to="/admin/cobertura">Cobertura e fontes</Link></nav>
    <div className="admin-content" key={`${privateEpoch}:${location.pathname}`}>{content}</div>
  </main>
}
