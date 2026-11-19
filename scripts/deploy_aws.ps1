<#
.SYNOPSIS
    Despliega Radar de gastos en AWS desde Windows, o elimina el despliegue.

.DESCRIPTION
    1. Comprueba AWS CLI, Terraform, el entorno .venv y que haya una sesión de AWS abierta.
    2. Empaqueta las Lambdas (dist/app.zip y dist/layer.zip).
    3. Crea terraform.tfvars la primera vez y ejecuta terraform init + apply.
    4. Carga la vista de Ejemplo (idempotente) y da de alta al administrador en Cognito,
       que recibe por correo una contraseña temporal.

    Se puede ejecutar las veces que haga falta: si no hay cambios, no cambia nada.

.EXAMPLE
    .\scripts\deploy_aws.ps1 -AdminEmail tu@correo.com

.EXAMPLE
    .\scripts\deploy_aws.ps1 -Destroy
#>
[CmdletBinding()]
param(
    # Correo del administrador. También recibe las alertas (hay que confirmar la suscripción de SNS).
    [string]$AdminEmail,
    # Tenant del administrador: minúsculas, números y guiones.
    [string]$Tenant = "mi-empresa",
    [string]$Region = "us-east-1",
    # Perfil de AWS CLI; si se omite se usa el predeterminado.
    [string]$AwsProfile,
    [switch]$AutoApprove,
    [switch]$Destroy
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$TfDir = Join-Path $Root "infra\terraform\envs\dev"
$TfVars = Join-Path $TfDir "terraform.tfvars"
$Python = Join-Path $Root ".venv\Scripts\python.exe"

function Write-Step([string]$Text) {
    Write-Host ""
    Write-Host "==> $Text" -ForegroundColor Cyan
}

# Ejecuta un programa externo y se detiene si falla.
function Invoke-Native([string]$Exe, [string[]]$Arguments) {
    & $Exe @Arguments
    if ($LASTEXITCODE -ne 0) { throw "Falló: $Exe $($Arguments -join ' ')" }
}

# Ejecuta un programa externo sin detenerse; devuelve su salida y deja $LASTEXITCODE.
function Invoke-Quiet([string]$Exe, [string[]]$Arguments) {
    $previous = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try { & $Exe @Arguments 2>$null } finally { $ErrorActionPreference = $previous }
}

function Get-TfOutput([string]$Name) {
    $value = Invoke-Quiet "terraform" @("output", "-raw", $Name)
    if ($LASTEXITCODE -ne 0) { throw "Terraform no tiene la salida '$Name'." }
    return ($value | Out-String).Trim()
}

# ---------------------------------------------------------------- requisitos
Write-Step "Comprobando requisitos"
if (-not (Get-Command aws -ErrorAction SilentlyContinue)) {
    throw "Falta AWS CLI. Instálalo con: winget install Amazon.AWSCLI"
}
if (-not (Get-Command terraform -ErrorAction SilentlyContinue)) {
    throw "Falta Terraform. Instálalo con: winget install Hashicorp.Terraform (y abre una terminal nueva)."
}
if (-not (Test-Path $Python)) {
    throw "Falta el entorno .venv. Ejecuta: py -3.13 -m venv .venv; .venv\Scripts\python -m pip install -e "".[dev]"""
}
if ($Tenant -notmatch '^[a-z0-9][a-z0-9-]{1,62}$') {
    throw "Tenant no válido: usa minúsculas, números y guiones (p. ej. mi-empresa)."
}

# La región de un despliegue existente manda sobre el parámetro.
if (Test-Path $TfVars) {
    $match = Select-String -Path $TfVars -Pattern '^\s*region\s*=\s*"([^"]+)"' | Select-Object -First 1
    if ($match) { $Region = $match.Matches[0].Groups[1].Value }
}
if ($AwsProfile) { $env:AWS_PROFILE = $AwsProfile }
$env:AWS_REGION = $Region
$env:AWS_DEFAULT_REGION = $Region

$account = Invoke-Quiet "aws" @("sts", "get-caller-identity", "--query", "Account", "--output", "text")
if ($LASTEXITCODE -ne 0 -or -not $account) {
    throw "No hay una sesión de AWS abierta. Ejecuta 'aws login' (o 'aws configure sso') y vuelve a intentarlo."
}
Write-Host "Cuenta de AWS: $(($account | Out-String).Trim())   Región: $Region"

# ---------------------------------------------------------------- eliminar
if ($Destroy) {
    Write-Step "Eliminando el despliegue"
    # Terraform calcula el hash de los paquetes aunque vaya a borrar: basta con que existan.
    $dist = Join-Path $Root "dist"
    New-Item -ItemType Directory -Force $dist | Out-Null
    foreach ($zip in "app.zip", "layer.zip") {
        $path = Join-Path $dist $zip
        if (-not (Test-Path $path)) { New-Item -ItemType File $path | Out-Null }
    }
    Push-Location $TfDir
    try {
        Invoke-Native "terraform" @("init", "-input=false")
        $tfArgs = @("destroy", "-input=false")
        if ($AutoApprove) { $tfArgs += "-auto-approve" }
        Invoke-Native "terraform" $tfArgs
    } finally { Pop-Location }
    Write-Host "Listo: no queda infraestructura de Radar de gastos en la cuenta." -ForegroundColor Green
    return
}

# ---------------------------------------------------------------- configuración
if (-not (Test-Path $TfVars)) {
    if (-not $AdminEmail) { throw "La primera vez indica tu correo: -AdminEmail tu@correo.com" }
    Write-Step "Creando terraform.tfvars"
    # El dominio de Cognito debe ser único en todo AWS.
    $suffix = -join ((48..57) + (97..122) | Get-Random -Count 6 | ForEach-Object { [char]$_ })
    $content = @(
        "region                = ""$Region"""
        "environment           = ""dev"""
        "cognito_domain_prefix = ""radar-gastos-$suffix"""
        "alert_email           = ""$AdminEmail"""
    ) -join "`n"
    [System.IO.File]::WriteAllText($TfVars, "$content`n", (New-Object System.Text.UTF8Encoding $false))
    Write-Host "Guardado en $TfVars (no se sube a Git)."
}

# ---------------------------------------------------------------- empaquetado
Write-Step "Empaquetando las Lambdas (arm64)"
Push-Location $Root
try { Invoke-Native $Python @("scripts\build_lambda.py") } finally { Pop-Location }

# ---------------------------------------------------------------- infraestructura
Write-Step "Desplegando con Terraform (revisa el plan antes de aceptar)"
Push-Location $TfDir
try {
    Invoke-Native "terraform" @("init", "-input=false")
    $tfArgs = @("apply", "-input=false")
    if ($AutoApprove) { $tfArgs += "-auto-approve" }
    Invoke-Native "terraform" $tfArgs

    $table = Get-TfOutput "table_name"
    $pool = Get-TfOutput "user_pool_id"
    $dashboard = Get-TfOutput "dashboard_url"
    $api = Get-TfOutput "api_url"
    $ops = Get-TfOutput "ops_dashboard"
} finally { Pop-Location }

# ---------------------------------------------------------------- datos de ejemplo
Write-Step "Cargando la vista de Ejemplo"
Push-Location $Root
try { Invoke-Native $Python @("scripts\seed_demo.py", "--table", $table, "--region", $Region) } finally { Pop-Location }

# ---------------------------------------------------------------- administrador
if ($AdminEmail) {
    Write-Step "Dando de alta al administrador $AdminEmail"
    Invoke-Quiet "aws" @("cognito-idp", "admin-get-user", "--user-pool-id", $pool, "--username", $AdminEmail) | Out-Null
    if ($LASTEXITCODE -eq 0) {
        Write-Host "El usuario ya existe; se conserva su contraseña."
    } else {
        Invoke-Native "aws" @(
            "cognito-idp", "admin-create-user", "--user-pool-id", $pool, "--username", $AdminEmail,
            "--user-attributes", "Name=email,Value=$AdminEmail", "Name=email_verified,Value=true",
            "Name=custom:tenant_id,Value=$Tenant", "--desired-delivery-mediums", "EMAIL",
            "--query", "User.UserStatus", "--output", "text"
        )
        Write-Host "Cognito te envió una contraseña temporal por correo (válida 3 días)."
    }
    Invoke-Native "aws" @(
        "cognito-idp", "admin-add-user-to-group", "--user-pool-id", $pool,
        "--username", $AdminEmail, "--group-name", "admins"
    )
}

Write-Host ""
Write-Host "Radar de gastos está en AWS" -ForegroundColor Green
Write-Host "  Dashboard:  $dashboard"
Write-Host "  API:        $api"
Write-Host "  Operación:  $ops"
Write-Host ""
Write-Host "La primera vez CloudFront puede tardar unos minutos en responder."
Write-Host "Para borrar todo y dejar de pagar: .\scripts\deploy_aws.ps1 -Destroy"
