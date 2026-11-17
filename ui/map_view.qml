// Tactical map. Talks to Python only through the `bridge` context property
// (ui/map_view.py: MapBridge).
//
// Performance: the Python side pushes at most ~10 snapshots/s into `bridge.drones`.
// Here the snapshot is merged *incrementally* into a ListModel, so the marker and
// trail delegates are created once per drone and only their properties change.
import QtQuick
import QtLocation
import QtPositioning

Rectangle {
    id: root
    color: "#111827"

    ListModel { id: droneModel }
    property var trailCache: ({})

    // ------------------------------------------------------------------ plugin
    // Tiles come from config.yaml (map.tile_url) through the OSM plugin's custom
    // host. If your Qt build ships the MapLibre plugin (name: "maplibre") swap the
    // plugin block; every other item below is plugin agnostic.
    Plugin {
        id: mapPlugin
        name: "osm"
        PluginParameter { name: "osm.useragent"; value: "DroneSwarmCockpit/1.0" }
        PluginParameter { name: "osm.mapping.providersrepository.disabled"; value: true }
        PluginParameter { name: "osm.mapping.custom.host"; value: bridge.tileHost }
        PluginParameter { name: "osm.mapping.custom.datacopyright"; value: "OpenStreetMap contributors" }
        PluginParameter { name: "osm.mapping.custom.mapcopyright"; value: "" }
    }

    Map {
        id: map
        anchors.fill: parent
        plugin: mapPlugin
        zoomLevel: bridge.zoom
        minimumZoomLevel: 3
        maximumZoomLevel: 19
        copyrightsVisible: true

        Component.onCompleted: {
            center = QtPositioning.coordinate(bridge.centerLat, bridge.centerLon)
            root.pickMapType()
        }
        onSupportedMapTypesChanged: root.pickMapType()

        // ---- pan / zoom (Qt 6 handler based interaction)
        WheelHandler {
            acceptedDevices: Qt.platform.pluginName === "cocoa" || Qt.platform.pluginName === "wayland"
                             ? PointerDevice.Mouse | PointerDevice.TouchPad
                             : PointerDevice.Mouse
            rotationScale: 1 / 120
            property: "zoomLevel"
        }
        DragHandler {
            target: null
            onTranslationChanged: (delta) => map.pan(-delta.x, -delta.y)
        }
        PinchHandler {
            id: pinch
            target: null
            property real startZoom: 0
            onActiveChanged: if (active) startZoom = map.zoomLevel
            onScaleChanged: (delta) => map.zoomLevel += Math.log2(delta)
        }

        // ---- single click handler: markers are hit-tested here so a click on a
        //      drone never also drops a waypoint.
        TapHandler {
            acceptedButtons: Qt.LeftButton
            onTapped: (eventPoint) => root.handleTap(eventPoint.position)
        }

        // ---- geofences
        MapItemView {
            model: bridge.geofences
            delegate: MapCircle {
                z: 1
                center: QtPositioning.coordinate(modelData.lat, modelData.lon)
                radius: modelData.radius
                color: modelData.fill
                border.color: modelData.stroke
                border.width: 2
            }
        }

        // ---- trails (last N positions per drone)
        MapItemView {
            model: droneModel
            delegate: MapPolyline {
                z: 5
                line.width: 2
                line.color: model.markerColor
                opacity: model.stale ? 0.3 : 0.75
                path: model.trail ? JSON.parse(model.trail) : []
            }
        }

        // ---- waypoints
        MapItemView {
            model: bridge.waypoints
            delegate: MapQuickItem {
                z: 8
                coordinate: QtPositioning.coordinate(modelData.lat, modelData.lon)
                anchorPoint.x: wp.width / 2
                anchorPoint.y: wp.height / 2
                sourceItem: Rectangle {
                    id: wp
                    width: 22; height: 22
                    rotation: 45
                    color: "#F59E0B"
                    border.color: "#111827"; border.width: 2
                    Text {
                        anchors.centerIn: parent
                        rotation: -45
                        text: modelData.index
                        color: "#111827"
                        font.pixelSize: 11
                        font.bold: true
                    }
                }
            }
        }

        // ---- drone markers
        MapItemView {
            model: droneModel
            delegate: MapQuickItem {
                z: 10
                coordinate: QtPositioning.coordinate(model.lat, model.lon)
                anchorPoint.x: marker.width / 2
                anchorPoint.y: marker.height / 2
                sourceItem: Item {
                    id: marker
                    width: 38; height: 38
                    opacity: model.stale ? 0.5 : 1.0

                    Rectangle {                       // selection ring
                        anchors.centerIn: parent
                        width: 38; height: 38; radius: 19
                        color: "transparent"
                        border.color: "#FFFFFF"; border.width: 2
                        visible: model.selected
                    }
                    Rectangle {                       // role-coloured body
                        anchors.centerIn: parent
                        width: 26; height: 26; radius: 13
                        color: model.markerColor
                        border.color: "#111827"; border.width: 2
                        Text {
                            anchors.centerIn: parent
                            text: model.droneId
                            color: "white"
                            font.pixelSize: 11
                            font.bold: true
                        }
                    }
                }
            }
        }
    }

    // --------------------------------------------------------------- overlays
    Rectangle {
        visible: bridge.waypointMode
        anchors { top: parent.top; horizontalCenter: parent.horizontalCenter; topMargin: 10 }
        width: banner.implicitWidth + 24; height: 30; radius: 6
        color: "#CCB45309"
        Text {
            id: banner
            anchors.centerIn: parent
            text: "WAYPOINT MODE — click the map to drop a waypoint"
            color: "white"; font.pixelSize: 12; font.bold: true
        }
    }

    Rectangle {                                       // legend
        anchors { left: parent.left; bottom: parent.bottom; margins: 10 }
        width: legendRow.implicitWidth + 16; height: 28; radius: 6
        color: "#CC111827"
        Row {
            id: legendRow
            anchors.centerIn: parent
            spacing: 12
            Repeater {
                model: [
                    { t: "LEADER", c: "#8B5CF6" }, { t: "FOLLOWER", c: "#3B82F6" },
                    { t: "CANDIDATE", c: "#F59E0B" }, { t: "LORA_ONLY", c: "#14B8A6" },
                    { t: "LOST", c: "#EF4444" }
                ]
                delegate: Row {
                    spacing: 5
                    Rectangle { width: 10; height: 10; radius: 5; color: modelData.c; anchors.verticalCenter: parent.verticalCenter }
                    Text { text: modelData.t; color: "#E5E7EB"; font.pixelSize: 10 }
                }
            }
        }
    }

    Column {                                          // zoom buttons
        anchors { right: parent.right; top: parent.top; margins: 10 }
        spacing: 4
        Repeater {
            model: [{ s: "+", d: 1 }, { s: "−", d: -1 }]
            delegate: Rectangle {
                width: 30; height: 30; radius: 6; color: "#CC1F2937"
                border.color: "#4B5563"
                Text { anchors.centerIn: parent; text: modelData.s; color: "white"; font.pixelSize: 18 }
                TapHandler { onTapped: map.zoomLevel = Math.max(3, Math.min(19, map.zoomLevel + modelData.d)) }
            }
        }
    }

    // ------------------------------------------------------------------ logic
    Connections {
        target: bridge
        function onDronesChanged() { root.syncDrones() }
        function onCenterRequested(lat, lon) {
            map.center = QtPositioning.coordinate(lat, lon)
            if (map.zoomLevel < 15) map.zoomLevel = 15
        }
    }

    function pickMapType() {
        var types = map.supportedMapTypes
        for (var i = 0; i < types.length; i++) {
            if (types[i].style === MapType.CustomMap) { map.activeMapType = types[i]; return }
        }
        if (types.length > 0) map.activeMapType = types[0]   // fall back to stock OSM street map
    }

    function indexOfDrone(id) {
        for (var i = 0; i < droneModel.count; i++)
            if (droneModel.get(i).droneId === id) return i
        return -1
    }

    // Merge bridge.drones into the ListModel without recreating delegates.
    function syncDrones() {
        var list = bridge.drones
        var seen = {}
        for (var i = 0; i < list.length; i++) {
            var d = list[i]
            seen[d.id] = true
            var row = { droneId: d.id, lat: d.lat, lon: d.lon, markerColor: d.color,
                        stale: d.stale, selected: d.selected }
            var idx = indexOfDrone(d.id)
            if (idx < 0) {
                row.trail = d.trail
                droneModel.append(row)
            } else {
                if (d.trail !== trailCache[d.id]) row.trail = d.trail   // only touch when changed
                droneModel.set(idx, row)
            }
            trailCache[d.id] = d.trail
        }
        for (var j = droneModel.count - 1; j >= 0; j--) {
            var id = droneModel.get(j).droneId
            if (!seen[id]) { droneModel.remove(j); delete trailCache[id] }
        }
    }

    function handleTap(pos) {
        var best = -1, bestDist = 24
        for (var i = 0; i < droneModel.count; i++) {
            var m = droneModel.get(i)
            var p = map.fromCoordinate(QtPositioning.coordinate(m.lat, m.lon), false)
            var dist = Math.hypot(p.x - pos.x, p.y - pos.y)
            if (dist < bestDist) { bestDist = dist; best = m.droneId }
        }
        if (best >= 0) { bridge.droneClicked(best); return }
        var c = map.toCoordinate(pos, false)
        bridge.mapClicked(c.latitude, c.longitude)
    }
}
