use super::*;

#[test]
fn drag_path_schema_advertises_bounded_desktop_intermediate_points() {
    let tools = tool_definitions();
    let act = tools
        .iter()
        .find(|tool| tool["name"] == "computer_act")
        .unwrap();
    let action = &act["inputSchema"]["properties"]["action"];
    let path = &action["properties"]["path"];
    assert_eq!(path["type"], "array");
    assert_eq!(path["minItems"], 1);
    assert_eq!(path["maxItems"], 64);
    assert_eq!(path["items"]["required"], json!(["x", "y"]));
    assert_eq!(path["items"]["additionalProperties"], false);
    assert_eq!(path["items"]["properties"]["x"]["type"], "number");
    assert_eq!(path["items"]["properties"]["y"]["type"], "number");
    let description = path["description"].as_str().unwrap();
    assert!(description.contains("intermediate") && description.contains("desktop"));
    assert_eq!(action["properties"]["end_x"]["type"], "number");
    assert_eq!(action["properties"]["end_y"]["type"], "number");
}
