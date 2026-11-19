<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { RouterLink } from 'vue-router'
import { api, type ModelProviderInput } from '@/api/client'
import { errorText, formatDate } from '@/api/format'
import type { ModelDefault, ModelProbeResult, ModelProvider, ModelProviderKind } from '@/api/types'
import { toast } from '@/state/toast'

/**
 * 设置 · 模型服务商（GET/POST/PATCH/DELETE /api/v1/model-providers）。
 *
 * 出片用哪个模型是服务端的一条默认记录：这一页负责写，出片页只读，两处不各存一份。
 * 拉取清单失败时后端 502 的 message 里带着 base_url 和真实原因，这里原样贴出来，
 * 同时留一条手填通路（PUT /model-providers/{id}/models）：拉不到清单不等于用不了。
 */
interface KindPreset {
  value: ModelProviderKind
  label: string
  baseUrl: string
}

const KIND_PRESETS: KindPreset[] = [
  { value: 'deepseek', label: 'DeepSeek', baseUrl: 'https://api.deepseek.com/v1' },
  { value: 'openai', label: 'OpenAI', baseUrl: 'https://api.openai.com/v1' },
  { value: 'ollama', label: 'Ollama', baseUrl: 'http://127.0.0.1:11434/v1' },
  { value: 'openrouter', label: 'OpenRouter', baseUrl: 'https://openrouter.ai/api/v1' },
  { value: 'custom', label: '自定义', baseUrl: '' },
]

function presetOf(kind: string): KindPreset | undefined {
  return KIND_PRESETS.find((preset) => preset.value === kind)
}

function kindLabel(kind: string): string {
  return presetOf(kind)?.label ?? (kind || '未声明类型')
}

/** 服务商 kind 是后端存下来的字符串：预设里没有就落回「自定义」，别硬塞一个错的中文标签。 */
function toPresetKind(kind: string): ModelProviderKind {
  return presetOf(kind)?.value ?? 'custom'
}

function parseModels(text: string): string[] {
  const seen = new Set<string>()
  const models: string[] = []
  text.split(',').forEach((item) => {
    const model = item.trim()
    if (!model || seen.has(model)) return
    seen.add(model)
    models.push(model)
  })
  return models
}

interface ProviderForm {
  id: string
  name: string
  kind: ModelProviderKind
  baseUrl: string
  apiKey: string
  modelsText: string
  models: string[]
  modelsSource: string
}

const providers = ref<ModelProvider[]>([])
const currentDefault = ref<ModelDefault | null>(null)
const loading = ref(true)
const loadError = ref('')
const exporting = ref(false)
/** 同一张卡片一次只做一件事：并发点「拉取」和「设默认」会把列表写乱。 */
const busyId = ref('')
const busyAction = ref<'' | 'pull' | 'default' | 'delete' | 'probe'>('')
const chosenModel = reactive<Record<string, string>>({})
const cardError = reactive<Record<string, string>>({})
/** 测试对话的结论：ok=false 也要留在卡片里 —— toast 会自己消失，余额不足这种要能反复看。 */
const cardProbe = reactive<Record<string, ModelProbeResult>>({})

const formOpen = ref(false)
const formBusy = ref(false)
const formError = ref('')
const fetching = ref(false)
const fetchError = ref('')
const form = reactive<ProviderForm>({
  id: '',
  name: '',
  kind: 'deepseek',
  baseUrl: '',
  apiKey: '',
  modelsText: '',
  models: [],
  modelsSource: '',
})

const baseUrlPlaceholder = computed(() => presetOf(form.kind)?.baseUrl || 'https://api.example.com/v1')

function isDefaultProvider(provider: ModelProvider): boolean {
  return currentDefault.value?.provider_id === provider.id
}

function applyProvider(updated: ModelProvider) {
  providers.value = providers.value.map((item) => (item.id === updated.id ? updated : item))
}

function modelPreview(provider: ModelProvider): string[] {
  return provider.models.slice(0, 6)
}

/** 每张卡的下拉默认停在「当前默认模型」上，其它卡停在自己的第一个模型：少一次点击就能设默认。 */
function syncChoices() {
  providers.value.forEach((provider) => {
    if (isDefaultProvider(provider) && currentDefault.value) {
      chosenModel[provider.id] = currentDefault.value.model
      return
    }
    if (!chosenModel[provider.id] && provider.models.length) chosenModel[provider.id] = provider.models[0]
  })
}

async function load(silent = false) {
  if (!silent) loading.value = true
  try {
    const list = await api.listModelProviders()
    providers.value = list.items
    currentDefault.value = list.default
    loadError.value = ''
    syncChoices()
  } catch (error) {
    // 静默刷新只是删除后的对账：拉不到就把手上这份留着，别把整页换成错误页。
    if (silent) toast.warn(errorText(error))
    else {
      loadError.value = errorText(error)
      providers.value = []
      currentDefault.value = null
    }
  } finally {
    if (!silent) loading.value = false
  }
}

function releaseBusy() {
  busyId.value = ''
  busyAction.value = ''
}

async function pullModels(provider: ModelProvider) {
  if (busyId.value) return
  busyId.value = provider.id
  busyAction.value = 'pull'
  cardError[provider.id] = ''
  try {
    const result = await api.fetchModelProviderModels(provider.id)
    applyProvider({ ...provider, models: result.models, models_source: result.models_source })
    if (result.models.length) toast.ok('拉取到 ' + result.models.length + ' 个模型：' + provider.name)
    else toast.warn('服务商没返回模型清单：核对 base_url，或在「编辑」里手填模型名')
  } catch (error) {
    // 502 的 message 里有 base_url 和原因，改写会让用户没法判断是地址还是 key 的问题。
    cardError[provider.id] = errorText(error)
  } finally {
    releaseBusy()
  }
}

/**
 * 真发一次对话再下结论：GET /models 有 key 就 200，余额不足/额度用尽只有对话才暴露。
 * 探测结果本身按 200 返回（它是结论不是错误），所以 ok=false 不算 catch，走卡片里的 alert。
 */
async function probeProvider(provider: ModelProvider) {
  if (busyId.value) return
  busyId.value = provider.id
  busyAction.value = 'probe'
  delete cardProbe[provider.id]
  cardError[provider.id] = ''
  try {
    // 用卡片里填的那个模型测：用户测的就是出片会用的那个，留空则后端按服务商默认挑。
    const result = await api.probeModelProvider(provider.id, (chosenModel[provider.id] || '').trim())
    if (result.ok) {
      toast.ok(result.message)
      return
    }
    cardProbe[provider.id] = result
  } catch (error) {
    // 只剩两种：provider 不存在（404），或课堂服务本身连不上。
    cardProbe[provider.id] = { ok: false, status: 0, message: errorText(error), hint: '' }
  } finally {
    releaseBusy()
  }
}

async function setDefault(provider: ModelProvider) {
  if (busyId.value) return
  const model = (chosenModel[provider.id] || '').trim()
  if (!model) {
    toast.warn('先挑一个模型：从清单里选，或直接手填模型名')
    return
  }
  busyId.value = provider.id
  busyAction.value = 'default'
  cardError[provider.id] = ''
  try {
    const result = await api.setModelDefault(provider.id, model)
    currentDefault.value = { provider_id: result.provider_id, model: result.model, provider: result.provider }
    applyProvider(result.provider)
    chosenModel[provider.id] = result.model
    if (result.warning) toast.warn(result.warning)
    else toast.ok('默认模型：' + result.provider.name + ' · ' + result.model)
  } catch (error) {
    cardError[provider.id] = errorText(error)
  } finally {
    releaseBusy()
  }
}

async function remove(provider: ModelProvider) {
  if (busyId.value) return
  if (!window.confirm('删除服务商「' + provider.name + '」？它的 key 与模型清单会一起删掉，出片就少一条可用通路。')) return
  busyId.value = provider.id
  busyAction.value = 'delete'
  try {
    await api.deleteModelProvider(provider.id)
    // 删掉的就是默认那张时先清本地默认：列表里的「默认」标记不该等下一次请求才消失。
    if (isDefaultProvider(provider)) currentDefault.value = null
    delete chosenModel[provider.id]
    delete cardError[provider.id]
    toast.ok('已删除服务商：' + provider.name)
    await load(true)
  } catch (error) {
    cardError[provider.id] = errorText(error)
  } finally {
    releaseBusy()
  }
}

async function exportEnv() {
  if (exporting.value) return
  exporting.value = true
  try {
    const result = await api.exportModelEnv()
    if (!result.written.length) {
      // 没写成任何键只有一种原因：库里还没有默认服务商。这时报"已写入…没有可写的键"
      // 既像成功又像失败，用户会以为按钮坏了 —— 直接说下一步该点什么。
      toast.warn('还没有设置默认模型，没有可写的键：先在卡片里选一个模型、点「设为默认」')
      return
    }
    toast.ok('已写入 ' + result.path + '：' + result.written.join('、'))
    if (result.skipped.length) toast.warn('跳过：' + result.skipped.join('、'))
  } catch (error) {
    toast.error(errorText(error))
  } finally {
    exporting.value = false
  }
}

function openCreate() {
  Object.assign(form, {
    id: '',
    name: '',
    kind: 'deepseek' as ModelProviderKind,
    baseUrl: KIND_PRESETS[0].baseUrl,
    apiKey: '',
    modelsText: '',
    models: [],
    modelsSource: '',
  })
  formError.value = ''
  fetchError.value = ''
  formOpen.value = true
}

function openEdit(provider: ModelProvider) {
  Object.assign(form, {
    id: provider.id,
    name: provider.name,
    kind: toPresetKind(provider.kind),
    baseUrl: provider.base_url,
    apiKey: '',
    modelsText: provider.models_source === 'manual' ? provider.models.join(', ') : '',
    models: [...provider.models],
    modelsSource: provider.models_source,
  })
  formError.value = ''
  fetchError.value = ''
  formOpen.value = true
}

function closeForm() {
  if (formBusy.value || fetching.value) return
  formOpen.value = false
}

/** 切类型顺手填上预设地址，但用户自己敲的地址不动：覆盖手填内容是不可撤销的破坏。 */
function onKindChange() {
  const preset = presetOf(form.kind)
  if (!preset) return
  const typed = form.baseUrl.trim()
  const typedByUser = typed !== '' && !KIND_PRESETS.some((item) => item.baseUrl === typed)
  if (!typedByUser) form.baseUrl = preset.baseUrl
}

async function fetchModels() {
  if (fetching.value || formBusy.value) return
  if (!form.id) {
    fetchError.value = '先把这次新增保存下来，再回头拉取模型'
    return
  }
  fetching.value = true
  fetchError.value = ''
  try {
    const result = await api.fetchModelProviderModels(form.id)
    form.models = result.models
    form.modelsSource = result.models_source
    const target = providers.value.find((item) => item.id === form.id)
    if (target) applyProvider({ ...target, models: result.models, models_source: result.models_source })
    if (result.models.length) toast.ok('拉取到 ' + result.models.length + ' 个模型')
    else fetchError.value = '服务商没返回模型清单：核对 base_url，或在下面手填模型名'
  } catch (error) {
    fetchError.value = errorText(error)
  } finally {
    fetching.value = false
  }
}

async function saveManualModels() {
  if (formBusy.value || !form.id) return
  const models = parseModels(form.modelsText)
  if (!models.length) {
    formError.value = '手填模型用英文逗号分隔，例如 deepseek-chat, deepseek-reasoner'
    return
  }
  formBusy.value = true
  formError.value = ''
  try {
    const updated = await api.saveModelProviderModels(form.id, models)
    form.models = updated.models
    form.modelsSource = updated.models_source
    applyProvider(updated)
    toast.ok('已保存 ' + updated.models.length + ' 个手填模型')
  } catch (error) {
    formError.value = errorText(error)
  } finally {
    formBusy.value = false
  }
}

async function save() {
  if (formBusy.value) return
  const name = form.name.trim()
  const baseUrl = form.baseUrl.trim()
  const apiKey = form.apiKey.trim()
  if (!name) {
    formError.value = '给服务商起个名字：列表里靠它区分'
    return
  }
  if (!baseUrl) {
    formError.value = '填上 base_url：拉模型和出片都发到这里'
    return
  }
  if (!form.id && !apiKey) {
    formError.value = '填上 api_key：新建的服务商要能真的调通'
    return
  }
  formBusy.value = true
  formError.value = ''
  try {
    if (form.id) {
      const input: ModelProviderInput = { name, kind: form.kind, base_url: baseUrl }
      // 留空＝不改动已存的 key：把空串传上去会把用户的 key 清掉。
      if (apiKey) input.api_key = apiKey
      const updated = await api.updateModelProvider(form.id, input)
      applyProvider(updated)
      const manual = parseModels(form.modelsText)
      // 编辑时手填了清单却没点「保存手填模型」也算数：静默丢掉用户输入比多一次 PUT 更糟。
      if (manual.length) applyProvider(await api.saveModelProviderModels(updated.id, manual))
      toast.ok('已保存服务商：' + updated.name)
    } else {
      const models = parseModels(form.modelsText)
      // 新建时手填的清单跟创建一起提交：服务商一落地就有模型可选，不用再补一次 PUT。
      const created = await api.createModelProvider({
        name,
        kind: form.kind,
        base_url: baseUrl,
        api_key: apiKey,
        models,
        models_source: models.length ? 'manual' : '',
      })
      toast.ok('已新增服务商：' + created.name)
      await load(true)
    }
    formOpen.value = false
  } catch (error) {
    formError.value = errorText(error)
  } finally {
    formBusy.value = false
  }
}

onMounted(() => void load())
</script>

<template>
  <div class="classroom-page">
    <header class="library-heading">
      <div>
        <span class="eyebrow">MODEL PROVIDERS</span>
        <h1 class="page-title">设置</h1>
        <p class="lead">接上模型服务商：拉取模型清单、指定默认模型，并把默认写回 .env。出片页只读这条默认。</p>
      </div>
      <div class="heading-actions">
        <button class="btn" type="button" :disabled="exporting" @click="exportEnv">
          <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 4v10m-4.2-4.2L12 14l4.2-4.2M4.5 17.5v1A1.5 1.5 0 0 0 6 20h12a1.5 1.5 0 0 0 1.5-1.5v-1" /></svg>
          {{ exporting ? '写入中…' : '写入 .env' }}
        </button>
        <button class="btn btn-primary" type="button" @click="openCreate">
          <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 5v14M5 12h14" /></svg>
          新增服务商
        </button>
      </div>
    </header>

    <div v-if="loading" class="library-loading" role="status">
      <span class="loading-bar" aria-hidden="true" />
      <span>正在读取模型服务商…</span>
    </div>

    <section v-else-if="loadError" class="placeholder-block">
      <h2>服务商列表没有响应</h2>
      <p>{{ loadError }}。<br>请确认课堂服务在运行（<code>python app.py</code>），然后重试。</p>
      <div class="placeholder-actions">
        <button class="btn btn-primary" type="button" @click="load()">重试</button>
        <RouterLink class="btn" :to="{ name: 'app-generate' }">回到生成课堂</RouterLink>
      </div>
    </section>

    <template v-else>
      <section class="default-banner" aria-label="当前默认模型">
        <span class="banner-icon" aria-hidden="true">
          <svg viewBox="0 0 24 24">
            <circle cx="12" cy="12" r="3.1" />
            <path d="M12 4v2M12 18v2M4.9 12h-2M21.1 12h-2M6.7 6.7 5.3 5.3M18.7 18.7l-1.4-1.4M17.3 6.7l1.4-1.4M5.3 18.7l1.4-1.4" />
          </svg>
        </span>
        <div class="banner-copy">
          <strong v-if="currentDefault">{{ currentDefault.provider.name }} · {{ currentDefault.model }}</strong>
          <strong v-else>还没设置默认模型</strong>
          <span v-if="currentDefault">{{ currentDefault.provider.base_url }}</span>
          <span v-else>在下面任一张卡片上挑一个模型，点「设为默认」；出片页会立刻读到它。</span>
        </div>
        <span class="banner-mark">{{ currentDefault ? 'DEFAULT MODEL' : 'NOT CONFIGURED' }}</span>
      </section>

      <section v-if="providers.length" class="provider-grid" aria-label="模型服务商">
        <article
          v-for="provider in providers"
          :key="provider.id"
          class="provider-card"
          :class="{ 'provider-card--default': isDefaultProvider(provider) }"
        >
          <header class="provider-head">
            <div class="provider-title">
              <h2>{{ provider.name }}</h2>
              <span class="provider-kind">{{ kindLabel(provider.kind) }}</span>
            </div>
            <span v-if="isDefaultProvider(provider)" class="default-badge">默认</span>
          </header>

          <dl class="provider-meta">
            <div>
              <dt>接口地址</dt>
              <dd class="mono">{{ provider.base_url || '—' }}</dd>
            </div>
            <div>
              <dt>API Key</dt>
              <dd class="mono">{{ provider.has_key ? provider.api_key_masked : '未配置' }}</dd>
            </div>
            <div>
              <dt>模型</dt>
              <dd>{{ provider.models.length }} 个<template v-if="provider.models_source === 'manual'">（手填）</template><template v-else-if="provider.models_source === 'provider'">（已拉取）</template></dd>
            </div>
          </dl>

          <div class="model-chips">
            <span v-for="model in modelPreview(provider)" :key="model" class="model-chip mono">{{ model }}</span>
            <span v-if="provider.models.length > modelPreview(provider).length" class="model-chip">+{{ provider.models.length - modelPreview(provider).length }}</span>
            <span v-if="!provider.models.length" class="field-hint">还没有模型清单：拉取或在「编辑」里手填。</span>
          </div>

          <div class="default-row">
            <label class="default-field">
              <span class="field-label">默认模型</span>
              <input
                v-model="chosenModel[provider.id]"
                class="provider-input mono"
                type="text"
                :list="'models-' + provider.id"
                :placeholder="provider.models[0] || '先拉取或手填模型名'"
                :aria-label="'默认模型：' + provider.name"
              >
              <span v-if="!currentDefault" class="field-hint">
                填/选好模型后要点右边「设为默认」才会生效：只填在框里不写库，出片仍会提示"还没有服务商"。
              </span>
              <datalist :id="'models-' + provider.id">
                <option v-for="model in provider.models" :key="model" :value="model" />
              </datalist>
            </label>
            <button
              class="btn btn-sm"
              type="button"
              :disabled="busyId === provider.id"
              @click="setDefault(provider)"
            >
              {{ busyId === provider.id && busyAction === 'default' ? '设置中…' : '设为默认' }}
            </button>
          </div>

          <p v-if="cardError[provider.id]" class="provider-error" role="alert">
            {{ cardError[provider.id] }}
            <button class="link-button" type="button" @click="openEdit(provider)">改成手填模型</button>
          </p>

          <p v-if="cardProbe[provider.id]" class="provider-error" role="alert">
            {{ cardProbe[provider.id]?.message }}
            <span v-if="cardProbe[provider.id]?.hint" class="probe-hint">{{ cardProbe[provider.id]?.hint }}</span>
          </p>

          <footer class="provider-foot">
            <button class="btn btn-sm" type="button" :disabled="busyId === provider.id" @click="openEdit(provider)">编辑</button>
            <button class="btn btn-sm" type="button" :disabled="busyId === provider.id" @click="pullModels(provider)">
              {{ busyId === provider.id && busyAction === 'pull' ? '拉取中…' : '拉取模型' }}
            </button>
            <button class="btn btn-sm" type="button" :disabled="busyId === provider.id" @click="probeProvider(provider)">
              {{ busyId === provider.id && busyAction === 'probe' ? '测试中…' : '测试对话' }}
            </button>
            <span class="provider-foot-meta">更新于 {{ formatDate(provider.updated_at) }}</span>
            <button class="btn btn-sm provider-danger" type="button" :disabled="busyId === provider.id" @click="remove(provider)">
              {{ busyId === provider.id && busyAction === 'delete' ? '删除中…' : '删除' }}
            </button>
          </footer>
        </article>
      </section>

      <section v-else class="placeholder-block">
        <h2>还没有模型服务商</h2>
        <p>新增一个服务商（DeepSeek / OpenAI / Ollama / OpenRouter / 自定义），拉取模型清单后设为默认，出片就开始用它。</p>
        <div class="placeholder-actions">
          <button class="btn btn-primary" type="button" @click="openCreate">新增服务商</button>
        </div>
      </section>
    </template>

    <div v-if="formOpen" class="modal-scrim" role="presentation" @click.self="closeForm()">
      <div class="modal-card" role="dialog" aria-modal="true" aria-labelledby="provider-form-title">
        <header class="modal-head">
          <div>
            <span class="modal-kicker">MODEL PROVIDER</span>
            <h2 id="provider-form-title">{{ form.id ? '编辑服务商' : '新增服务商' }}</h2>
          </div>
          <button class="modal-close" type="button" aria-label="关闭" :disabled="formBusy" @click="closeForm()">
            <svg viewBox="0 0 24 24" aria-hidden="true"><path d="m6 6 12 12M18 6 6 18" /></svg>
          </button>
        </header>

        <div class="field-grid">
          <label class="field">
            <span class="field-label">名称</span>
            <input v-model="form.name" class="provider-input" type="text" maxlength="40" placeholder="例如 DeepSeek 官方" aria-label="服务商名称" :disabled="formBusy">
          </label>
          <label class="field">
            <span class="field-label">类型</span>
            <select v-model="form.kind" class="provider-input" aria-label="服务商类型" :disabled="formBusy" @change="onKindChange">
              <option v-for="preset in KIND_PRESETS" :key="preset.value" :value="preset.value">{{ preset.label }}</option>
            </select>
          </label>
        </div>

        <label class="field">
          <span class="field-label">base_url</span>
          <input v-model="form.baseUrl" class="provider-input mono" type="text" :placeholder="baseUrlPlaceholder" aria-label="base_url" :disabled="formBusy">
        </label>

        <label class="field">
          <span class="field-label">api_key</span>
          <input
            v-model="form.apiKey"
            class="provider-input mono"
            type="password"
            autocomplete="off"
            aria-label="api_key"
            :placeholder="form.id ? '留空 = 不改动已存的 key' : 'sk-…'"
            :disabled="formBusy"
          >
        </label>
        <p class="field-hint">key 只在提交时上送，列表里永远是掩码。{{ form.id ? '编辑时留空表示不改动已存的 key。' : '' }}</p>

        <section class="model-block" aria-label="模型">
          <div class="model-block-head">
            <strong>模型</strong>
            <span>{{ form.models.length }} 个<template v-if="form.modelsSource === 'manual'"> · 手填</template><template v-else-if="form.modelsSource === 'provider'"> · 已拉取</template></span>
          </div>
          <div class="model-chips">
            <span v-for="model in form.models" :key="model" class="model-chip mono">{{ model }}</span>
            <span v-if="!form.models.length" class="field-hint">还没有清单：可以拉取，也可以手填。</span>
          </div>

          <div class="model-actions">
            <button class="btn btn-sm" type="button" :disabled="fetching || formBusy" @click="fetchModels">
              {{ fetching ? '拉取中…' : '从服务商拉取' }}
            </button>
            <span v-if="!form.id" class="field-hint">新增保存后才能拉取。</span>
          </div>
          <p v-if="fetchError" class="form-error" role="alert">{{ fetchError }}</p>

          <label class="field">
            <span class="field-label">手填模型（英文逗号分隔）</span>
            <input
              v-model="form.modelsText"
              class="provider-input mono"
              type="text"
              placeholder="deepseek-chat, deepseek-reasoner"
              aria-label="手填模型"
              :disabled="formBusy"
            >
          </label>
          <div class="model-actions">
            <button class="btn btn-sm" type="button" :disabled="!form.id || formBusy" @click="saveManualModels">保存手填模型</button>
            <span class="field-hint">{{ form.id ? '拉取失败时走这条：保存后立刻写进这份服务商，不用再点下面的保存。' : '新增会把手填清单一起提交。' }}</span>
          </div>
        </section>

        <p v-if="formError" class="form-error" role="alert">{{ formError }}</p>

        <footer class="modal-foot">
          <button class="btn" type="button" :disabled="formBusy" @click="closeForm()">取消</button>
          <button class="btn btn-primary" type="button" :disabled="formBusy" @click="save">
            {{ formBusy ? '保存中…' : form.id ? '保存修改' : '新增服务商' }}
          </button>
        </footer>
      </div>
    </div>
  </div>
</template>

<style scoped>
.classroom-page { max-width: 1160px; margin-inline: auto; }
.library-heading { display: flex; align-items: center; justify-content: space-between; gap: 24px; margin-bottom: 26px; }
.library-heading .page-title { margin-top: 7px; }
.library-heading .lead { margin-top: 7px; }
.heading-actions { display: flex; flex-wrap: wrap; align-items: center; gap: 9px; }
.heading-actions .btn { display: inline-flex; align-items: center; gap: 7px; white-space: nowrap; }
.heading-actions .btn svg { width: 15px; height: 15px; fill: none; stroke: currentColor; stroke-linecap: round; stroke-linejoin: round; stroke-width: 1.7; }

.library-loading { display: flex; align-items: center; gap: 10px; padding: 40px 4px; color: var(--muted); font-size: 12px; }
.loading-bar { width: 42px; height: 2px; border-radius: 2px; background: linear-gradient(90deg, var(--border), var(--accent), var(--border)); background-size: 200% 100%; animation: bar 1.2s linear infinite; }
@keyframes bar { to { background-position: -200% 0; } }

.default-banner { display: flex; align-items: center; gap: 14px; min-height: 84px; margin-bottom: 26px; padding: 16px 20px; border: 1px solid color-mix(in oklch, var(--accent) 13%, var(--border)); border-radius: 16px; background: linear-gradient(105deg, color-mix(in oklch, var(--accent) 7%, var(--surface)), var(--surface) 66%); }
.banner-icon { display: grid; width: 46px; height: 46px; flex: none; place-items: center; border: 1px solid color-mix(in oklch, var(--accent) 20%, var(--border)); border-radius: 14px; background: color-mix(in oklch, var(--accent) 10%, var(--surface)); color: var(--accent); }
.banner-icon svg { width: 23px; height: 23px; fill: none; stroke: currentColor; stroke-linecap: round; stroke-linejoin: round; stroke-width: 1.55; }
.banner-copy { display: flex; flex-direction: column; gap: 4px; min-width: 0; }
.banner-copy strong { font-size: 13px; font-weight: 650; overflow-wrap: anywhere; }
.banner-copy span { color: var(--muted); font-size: 12px; line-height: 1.6; overflow-wrap: anywhere; }
.banner-mark { margin-left: auto; flex: none; color: color-mix(in oklch, var(--accent) 35%, var(--meta)); font-family: var(--font-mono); font-size: 10px; letter-spacing: .09em; }

.provider-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 18px; align-items: start; }
.provider-card { min-width: 0; padding: 16px 17px; border: 1px solid var(--border); border-radius: 15px; background: var(--surface); }
.provider-card--default { border-color: color-mix(in oklch, var(--accent) 34%, var(--border)); box-shadow: 0 8px 24px color-mix(in oklch, var(--accent) 8%, transparent); }
.provider-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 10px; }
.provider-title { display: flex; flex-wrap: wrap; align-items: baseline; gap: 8px; min-width: 0; }
.provider-title h2 { font-size: 15px; font-weight: 650; letter-spacing: -.01em; overflow-wrap: anywhere; }
.provider-kind { padding: 2px 7px; border: 1px solid var(--border-soft); border-radius: 999px; background: var(--bg); color: var(--muted); font-size: 10px; }
.default-badge { flex: none; padding: 3px 8px; border-radius: 999px; background: color-mix(in oklch, var(--accent) 12%, var(--surface)); color: var(--accent); font-size: 10px; font-weight: 600; }

.provider-meta { display: flex; flex-direction: column; gap: 7px; margin-top: 13px; }
.provider-meta > div { display: grid; grid-template-columns: 62px minmax(0, 1fr); gap: 10px; align-items: baseline; }
.provider-meta dt { color: var(--meta); font-size: 10px; }
.provider-meta dd { margin: 0; color: var(--muted); font-size: 11px; overflow-wrap: anywhere; }

.model-chips { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 12px; }
.model-chip { padding: 4px 8px; border: 1px solid var(--border-soft); border-radius: 7px; background: var(--bg); color: var(--muted); font-size: 10px; overflow-wrap: anywhere; }

.default-row { display: flex; align-items: flex-end; gap: 9px; margin-top: 14px; padding-top: 13px; border-top: 1px solid var(--border-soft); }
.default-field { display: flex; flex: 1; min-width: 0; flex-direction: column; gap: 5px; }
.provider-input { width: 100%; min-width: 0; min-height: 34px; padding: 6px 10px; border: 1px solid var(--border); border-radius: 9px; background: var(--bg); color: var(--fg); font: inherit; font-size: 11px; }
.provider-input:focus { border-color: color-mix(in oklch, var(--accent) 45%, var(--border)); outline: 0; box-shadow: 0 0 0 3px color-mix(in oklch, var(--accent) 8%, transparent); }
.provider-input:disabled { cursor: not-allowed; opacity: .55; }
.provider-input::placeholder { color: var(--meta); }

.provider-error { margin: 12px 0 0; padding: 9px 11px; border: 1px solid color-mix(in oklch, var(--danger) 28%, var(--border)); border-radius: 9px; background: color-mix(in oklch, var(--danger) 5%, var(--surface)); color: var(--fg); font-size: 11px; line-height: 1.7; white-space: pre-wrap; overflow-wrap: anywhere; }
.probe-hint { display: block; margin-top: 4px; color: var(--meta); font-size: 10px; line-height: 1.7; }
.link-button { margin-left: 6px; padding: 0; border: 0; background: none; color: var(--accent); cursor: pointer; font: inherit; font-size: 11px; text-decoration: underline; }
.provider-foot { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; margin-top: 14px; }
.provider-foot-meta { margin-left: auto; color: var(--meta); font-size: 10px; }
.provider-danger { border-color: color-mix(in oklch, var(--danger) 30%, var(--border)); color: var(--danger); }
.provider-danger:hover:not(:disabled) { border-color: var(--danger); color: var(--danger); }

.placeholder-block { padding: 44px 20px; border: 1px solid var(--border); border-radius: 16px; background: var(--surface); text-align: center; }
.placeholder-block h2 { font-size: 17px; font-weight: 650; letter-spacing: -.02em; }
.placeholder-block p { max-width: 52ch; margin: 9px auto 0; color: var(--muted); font-size: 12px; line-height: 1.75; }
.placeholder-block code { padding: 1px 5px; border-radius: 5px; background: var(--bg); font-family: var(--font-mono); font-size: 10px; }
.placeholder-actions { display: flex; flex-wrap: wrap; justify-content: center; gap: 9px; margin-top: 18px; }

.field-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 11px; margin-top: 15px; }
.field { display: flex; flex-direction: column; gap: 5px; margin-top: 11px; }
.field-grid .field { margin-top: 0; }
.field-label { color: var(--meta); font-size: 10px; }
.field-hint { color: var(--meta); font-size: 10px; line-height: 1.6; overflow-wrap: anywhere; }

.model-block { margin-top: 15px; padding: 13px; border: 1px solid var(--border-soft); border-radius: 12px; background: var(--surface); }
.model-block-head { display: flex; align-items: baseline; justify-content: space-between; gap: 10px; }
.model-block-head strong { font-size: 12px; }
.model-block-head span { color: var(--meta); font-size: 10px; }
.model-actions { display: flex; flex-wrap: wrap; align-items: center; gap: 9px; margin-top: 10px; }
.model-block .field-hint { font-size: 10px; }

.form-error { margin: 11px 0 0; padding: 9px 11px; border: 1px solid color-mix(in oklch, var(--danger) 28%, var(--border)); border-radius: 9px; background: color-mix(in oklch, var(--danger) 5%, var(--bg)); color: var(--danger); font-size: 11px; line-height: 1.6; white-space: pre-wrap; overflow-wrap: anywhere; }

@media (max-width: 1000px) {
  .provider-grid { grid-template-columns: minmax(0, 1fr); }
}
@media (max-width: 760px) {
  .library-heading { flex-direction: column; align-items: flex-start; gap: 16px; }
  .banner-mark { display: none; }
  .field-grid { grid-template-columns: minmax(0, 1fr); }
  .default-row { flex-direction: column; align-items: stretch; }
  .provider-foot-meta { margin-left: 0; }
}
</style>
