import QtQuick 2.12
import QtQuick.Controls 2.15
import QtQuick.Layouts 1.15

Rectangle {
    anchors.fill: parent
    color: "grey"

    ListView {
        id: streamlist
        anchors.fill: parent
        spacing: 1
        model: MyModel
        anchors.margins: 1
        delegate:
            Rectangle {
                color: "lightgrey"
                width: parent ? parent.width : 0
                height: 80
                GridLayout {
                    anchors.margins: 2
                    clip: true
                    anchors.fill: parent
                    columns: 3
                    rows: 3
                    Text {
                        text: '<b>Name:</b> ' + name
                        Layout.row: 0; Layout.column: 0
                    }
                    Text {
                        text: '<b>Type:</b> ' + type
                        Layout.row: 0; Layout.column: 1
                    }
                    Row {
                        Layout.row: 0; Layout.column: 2
                        spacing: 5
                        Rectangle {
                            id: activityLight
                            width: 14
                            height: 14
                            radius: 7
                            border.width: 1
                            border.color: "#888"
                            color: {
                                if (activityState === 'active') return '#00ff00'
                                else if (activityState === 'warning') return '#ffaa00'
                                else if (activityState === 'critical') return '#ff0000'
                                else return 'gray'
                            }
                        }
                        CheckBox {
                            id: notifyCheckbox
                            checked: notifyEnabled
                            onCheckedChanged: {
                                OuterWidget.setNotifyEnabled(index, checked)
                            }
                        }
                    }
                    Text {
                        text: '<b>Host:</b> ' + hostname
                        Layout.row: 1; Layout.column: 0
                    }
                    Text {
                        text: '<b>Channels:</b> ' + channel_count + " (" + channel_format + ")"
                        Layout.row: 1; Layout.column: 1
                    }
                    Text {
                        text: '<b>Nom.Rate:</b> ' + nominal_srate
                        Layout.row: 2; Layout.column: 0
                    }
                    Text {
                        text: '<b>Eff.Rate:</b> ' + effective_rate
                        Layout.row: 2; Layout.column: 1
                    }
//                    Text {
//                        text: uid; elide: Text.ElideRight; Layout.preferredWidth: 150
//                        Layout.row: 2; Layout.column: 2
//                    }
                }
                MouseArea {
                    anchors.fill: parent
                    onClicked: streamlist.currentIndex = index
                    onDoubleClicked: OuterWidget.activated(index)  // console.warn("Double clicked " + index)
                }
                ListView.onAdd: {
                    OuterWidget.added(index)
                }
                ListView.onRemove: {
                    OuterWidget.removed()
                }
            }
        ScrollBar.vertical: ScrollBar { }
//        onCountChanged: console.warn('Model count has changed: ' + count)
//        onCurrentItemChanged: console.warn(streamlist.currentIndex + ' selected')
    }

    Button {
        width: 40
        height: 20
        anchors.bottom: parent.bottom
        anchors.right: parent.right
        Text {
            font.pointSize: 8
            text: "refresh"
            anchors.verticalCenter: parent.verticalCenter
        }
        onClicked: {
            MyModel.refresh();
        }
    }
}
