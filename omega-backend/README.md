# OMEGA Backend

Backend para el bot OMEGA v5.2 — maneja predicciones IA y ejecución automática en Polymarket.

## Endpoints

| Ruta | Método | Descripción |
|------|--------|-------------|
| `/claude` | POST | Proxy a Anthropic API (predicciones) |
| `/trade` | POST | Ejecuta orden en Polymarket con gestión de riesgo |
| `/status` | GET | Estado y estadísticas del bot |
| `/history` | GET | Historial de trades de la sesión |
| `/health` | GET | Health check |

---

## Deploy en Railway (recomendado, gratis)

### 1. Crear cuenta
Ve a [railway.app](https://railway.app) y crea cuenta con GitHub.

### 2. Nuevo proyecto
- Click **New Project → Deploy from GitHub repo**
- Selecciona este repositorio

### 3. Variables de entorno
En Railway → tu proyecto → **Variables**, agrega:

```
ANTHROPIC_API_KEY=sk-ant-...
POLY_API_KEY=           ← de polymarket.com/settings/api
POLY_SECRET=
POLY_PASSPHRASE=
POLY_WALLET=0x...
MAX_BET_USD=5.0
MIN_CONFIDENCE=65
BAD_HOURS=4,0,16,23,22,8,9
ALLOWED_ORIGINS=https://omegabot127.github.io
```

### 4. Obtener tu URL
Railway te da una URL tipo: `https://omega-backend-production.up.railway.app`

---

## Conectar con el HTML (index.html)

Busca esta línea en el HTML:
```javascript
const res = await fetch('https://omegabtc.site/claude', {
```

Cámbiala por tu nueva URL:
```javascript
const res = await fetch('https://TU-URL.up.railway.app/claude', {
```

Para activar el auto-trade, agrega esto en el HTML justo después de recibir la predicción:

```javascript
// Dentro de runFull(), después de renderPred(pred):
if (pred.direction !== 'NEUTRAL' && pred.confidence >= 65) {
  fetch('https://TU-URL.up.railway.app/trade', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      direction: pred.direction,
      confidence: pred.confidence,
      signal_quality: pred.signalQuality,
      token_up: polyData?.tokenUp,
      token_down: polyData?.tokenDown,
      bankroll: parseFloat(document.getElementById('brInput')?.value || '50'),
      hour: new Date().getHours()
    })
  })
  .then(r => r.json())
  .then(result => {
    if (result.executed) {
      console.log('✅ Trade ejecutado:', result.trade);
    } else {
      console.log('⏭️ Trade omitido:', result.reason);
    }
  })
  .catch(e => console.error('Trade error:', e));
}
```

---

## Gestión de riesgo incluida

El backend rechaza automáticamente trades cuando:
- Señal es **NEUTRAL**
- Confidence < 65% (configurable)
- Es una **hora mala** según tu historial (04h=17% WR, 00h=40%, etc.)
- Señal es **DÉBIL**

El tamaño de apuesta usa **Kelly Criterion** conservador (25% del Kelly completo).

---

## Modo simulación

Si no configuras `POLY_API_KEY`, el backend corre en **modo simulación** — registra todos los trades pero no ejecuta nada real. Ideal para validar primero.
