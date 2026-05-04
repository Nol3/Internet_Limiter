# Crear un nuevo release

Esta guía es para el dueño del repositorio. El workflow `.github/workflows/release.yml` automatiza el build y publicación de cada release.

## Flujo automatizado (recomendado)

### 1. Sube el repo a GitHub (solo la primera vez)

```powershell
cd "C:\Users\aleja\Desktop\Claude\Internet_Limiter-main"
git init
git add .
git commit -m "chore: initial commit (v1.0.0)"
git branch -M main
git remote add origin https://github.com/Nol3/Internet_Limiter.git
git push -u origin main
```

### 2. Verifica que `installer.iss` y `CHANGELOG.md` reflejan la versión correcta

- `installer.iss` línea: `#define MyAppVersion "1.0.0"`
- `CHANGELOG.md`: la sección `[1.0.0]` debe estar al inicio

### 3. Crea el tag y haz push

```powershell
git tag -a v1.0.0 -m "Release v1.0.0"
git push origin v1.0.0
```

GitHub Actions detecta el tag `v*` y dispara el workflow automáticamente:

1. Configura Python 3.12 + dependencias.
2. Genera `Limitador.ico` desde `Limiter_icon.png`.
3. Compila `dist\Limitador.exe` con PyInstaller.
4. Instala Inno Setup y compila `installer_output\Setup_Limitador_1.0.0.exe`.
5. Calcula checksums SHA-256.
6. Crea el GitHub Release y sube los 3 archivos.

### 4. Verifica el release publicado

Revisa <https://github.com/Nol3/Internet_Limiter/releases/latest>. Debes ver:

- `Limitador.exe`
- `Setup_Limitador_1.0.0.exe`
- `checksums.txt`

---

## Disparar el workflow manualmente

Si no quieres tag, puedes ejecutarlo manual:

1. Ve a la pestaña **Actions** del repo.
2. Selecciona "Build & Release" en la lista.
3. Click "Run workflow".
4. Introduce el tag (ej: `v1.0.0`).
5. Click "Run workflow".

---

## Crear un release manualmente (sin GitHub Actions)

```powershell
.\build_installer.bat
```

Sube `dist\Limitador.exe` y `installer_output\Setup_Limitador_1.0.0.exe` manualmente desde la UI de GitHub Releases.

---

## Bumping de versión para futuros releases

Cuando subas a v1.1.0, v2.0.0, etc.:

1. Edita `installer.iss`: `#define MyAppVersion "1.1.0"`
2. Añade una nueva sección al inicio de `CHANGELOG.md` con la fecha.
3. Actualiza el path hardcodeado en `.github/workflows/release.yml`:
   - `Setup_Limitador_1.0.0.exe` → `Setup_Limitador_1.1.0.exe` (3 ocurrencias).
4. Commit + tag + push:

```powershell
git add .
git commit -m "chore: bump to v1.1.0"
git tag -a v1.1.0 -m "Release v1.1.0"
git push origin main
git push origin v1.1.0
```

---

## Troubleshooting CI

### El workflow falla en "Build installer"

Inno Setup falló. Mira el log; suele ser un path roto en `installer.iss` (algún `Source: "..."` que no existe).

### El release se crea pero los archivos no se suben

Verifica que `permissions: contents: write` está en `release.yml` (ya lo está).

### `pydivert` no se empaqueta correctamente

PyInstaller debe usar `--collect-all pydivert` para incluir `WinDivert.dll` y `WinDivert.sys`. Ya está en el workflow.
