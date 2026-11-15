# CozyLife Control

Aplicación local para controlar switches CozyLife por TCP, sin Home Assistant ni nube.

## Ejecutar

```bash
cd ~/Documentos/cozylife-control
.venv/bin/python app.py
```

## Configuración

Los dispositivos se guardan en `devices.json`. El primer switch configurado es:

- Sala: `192.168.0.54:5555`

Desde la aplicación se pueden añadir, consultar, encender, apagar y eliminar switches.

## Crear acceso en el menú de aplicaciones

```bash
mkdir -p ~/.local/share/applications
cp cozylife-control.desktop ~/.local/share/applications/
chmod +x ~/.local/share/applications/cozylife-control.desktop
```

Después busca **CozyLife Control** en el menú de aplicaciones.

## Bandeja del sistema

En **Configuración** puedes activar:

- Iniciar al prender el equipo.
- Ocultar la ventana en la bandeja del sistema al cerrarla.

El menú del icono permite abrir la aplicación, actualizar los switches o salir completamente.

La aplicación usa el entorno virtual incluido en `.venv` para el soporte de bandeja.
