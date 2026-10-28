import { createApp } from 'vue'
import router from './router'
import './styles/prototype/tokens.css'
import './styles/prototype/base.css'
import './styles/prototype/app.css'
import './styles/prototype/landing.css'
import './styles/prototype/auth.css'
import './styles/workspace.css'
import './styles/deck.css'
import './styles/classroom.css'
import './style.css'
import App from './App.vue'
import { startAppState } from './state/app'
import faviconUrl from './assets/favicon.svg'

const icon = document.createElement('link')
icon.rel = 'icon'
icon.type = 'image/svg+xml'
icon.href = faviconUrl
document.head.appendChild(icon)

startAppState()
createApp(App).use(router).mount('#app')
