use crate::client::Client;
use serde_json::{json, Value};
use std::collections::HashMap;

type BoxError = Box<dyn std::error::Error>;

const THERMOSTAT_TYPE: &str = "sdm.devices.types.THERMOSTAT";

fn celsius_to_fahrenheit(c: f64) -> f64 {
    c * 9.0 / 5.0 + 32.0
}

fn fahrenheit_to_celsius(f: f64) -> f64 {
    (f - 32.0) * 5.0 / 9.0
}

fn get_trait<'a>(traits: &'a HashMap<String, Value>, name: &str) -> Option<&'a Value> {
    traits.get(&format!("sdm.devices.traits.{name}"))
}

pub async fn auth_login(client_secret: &str, project_id: &str) -> Result<(), BoxError> {
    crate::auth::login(client_secret, project_id).await
}

pub async fn list_devices() -> Result<(), BoxError> {
    let client = Client::new().await?;
    let devices = client.list_devices().await?;

    let thermostats: Vec<_> = devices
        .iter()
        .filter(|d| d.type_.as_deref() == Some(THERMOSTAT_TYPE))
        .collect();

    if thermostats.is_empty() {
        println!("No thermostats found.");
        return Ok(());
    }

    for device in thermostats {
        let name = device.name.as_deref().unwrap_or("unknown");
        let custom_name = device
            .traits
            .as_ref()
            .and_then(|t| t.get("sdm.devices.traits.Info"))
            .and_then(|v| v.get("customName"))
            .and_then(|v| v.as_str())
            .unwrap_or("(unnamed)");

        // Extract just the device ID portion for convenience
        let short_id = name.rsplit('/').next().unwrap_or(name);
        println!("{short_id}  {custom_name}");
    }

    Ok(())
}

pub async fn device_status(id: &str) -> Result<(), BoxError> {
    let client = Client::new().await?;
    let device = client.get_device(id).await?;

    let traits = device.traits.as_ref().ok_or("Device has no traits")?;

    let custom_name = get_trait(traits, "Info")
        .and_then(|v| v.get("customName"))
        .and_then(|v| v.as_str())
        .unwrap_or("(unnamed)");

    println!("Name: {custom_name}");

    if let Some(temp_c) = get_trait(traits, "Temperature")
        .and_then(|v| v.get("ambientTemperatureCelsius"))
        .and_then(|v| v.as_f64())
    {
        println!("Temperature: {:.1}°F ({:.1}°C)", celsius_to_fahrenheit(temp_c), temp_c);
    }

    if let Some(humidity) = get_trait(traits, "Humidity")
        .and_then(|v| v.get("ambientHumidityPercent"))
        .and_then(|v| v.as_f64())
    {
        println!("Humidity: {humidity:.0}%");
    }

    if let Some(mode) = get_trait(traits, "ThermostatMode")
        .and_then(|v| v.get("mode"))
        .and_then(|v| v.as_str())
    {
        println!("Mode: {mode}");
    }

    if let Some(hvac_status) = get_trait(traits, "ThermostatHvac")
        .and_then(|v| v.get("status"))
        .and_then(|v| v.as_str())
    {
        println!("HVAC: {hvac_status}");
    }

    // Show setpoints
    if let Some(setpoint) = get_trait(traits, "ThermostatTemperatureSetpoint") {
        if let Some(heat_c) = setpoint.get("heatCelsius").and_then(|v| v.as_f64()) {
            println!("Heat setpoint: {:.1}°F ({:.1}°C)", celsius_to_fahrenheit(heat_c), heat_c);
        }
        if let Some(cool_c) = setpoint.get("coolCelsius").and_then(|v| v.as_f64()) {
            println!("Cool setpoint: {:.1}°F ({:.1}°C)", celsius_to_fahrenheit(cool_c), cool_c);
        }
    }

    if let Some(eco) = get_trait(traits, "ThermostatEco")
        .and_then(|v| v.get("mode"))
        .and_then(|v| v.as_str())
    {
        println!("Eco: {eco}");
    }

    if let Some(connectivity) = get_trait(traits, "Connectivity")
        .and_then(|v| v.get("status"))
        .and_then(|v| v.as_str())
    {
        println!("Connectivity: {connectivity}");
    }

    Ok(())
}

pub async fn set_temperature(id: &str, temp_f: f64) -> Result<(), BoxError> {
    let client = Client::new().await?;

    // Determine current mode to pick the right command
    let device = client.get_device(id).await?;
    let traits = device.traits.as_ref().ok_or("Device has no traits")?;

    let mode = get_trait(traits, "ThermostatMode")
        .and_then(|v| v.get("mode"))
        .and_then(|v| v.as_str())
        .unwrap_or("HEAT");

    let temp_c = fahrenheit_to_celsius(temp_f);
    let mut params = HashMap::new();

    let command = match mode {
        "COOL" => {
            params.insert("coolCelsius".to_string(), json!(temp_c));
            "sdm.devices.commands.ThermostatTemperatureSetpoint.SetCool"
        }
        "HEATCOOL" => {
            return Err(
                "In HEATCOOL mode, use separate heat/cool setpoints. \
                 Switch to HEAT or COOL mode first, or set range via the Google Home app."
                    .into(),
            );
        }
        _ => {
            // Default to SetHeat for HEAT mode (and as fallback)
            params.insert("heatCelsius".to_string(), json!(temp_c));
            "sdm.devices.commands.ThermostatTemperatureSetpoint.SetHeat"
        }
    };

    client.execute_command(id, command, params).await?;
    println!("Set temperature to {temp_f:.0}°F ({temp_c:.1}°C)");
    Ok(())
}

pub async fn set_mode(id: &str, mode: &str) -> Result<(), BoxError> {
    let mode_upper = match mode.to_lowercase().as_str() {
        "heat" => "HEAT",
        "cool" => "COOL",
        "heatcool" => "HEATCOOL",
        "off" => "OFF",
        other => return Err(format!("Unknown mode: {other}. Use heat, cool, heatcool, or off.").into()),
    };

    let client = Client::new().await?;
    let mut params = HashMap::new();
    params.insert("mode".to_string(), json!(mode_upper));

    client
        .execute_command(id, "sdm.devices.commands.ThermostatMode.SetMode", params)
        .await?;

    println!("Mode set to {mode_upper}");
    Ok(())
}
