mod auth;
mod client;
mod commands;

use clap::{Parser, Subcommand};

#[derive(Parser)]
#[command(name = "nest-cli", about = "Control Google Nest thermostats via the SDM API")]
struct Cli {
    #[command(subcommand)]
    command: Commands,
}

#[derive(Subcommand)]
enum Commands {
    /// Manage authentication
    Auth {
        #[command(subcommand)]
        action: AuthAction,
    },
    /// Manage devices
    Devices {
        #[command(subcommand)]
        action: DeviceAction,
    },
    /// Set thermostat properties
    Set {
        #[command(subcommand)]
        action: SetAction,
    },
}

#[derive(Subcommand)]
enum AuthAction {
    /// Run OAuth2 login flow and store tokens
    Login {
        /// Path to the OAuth client secret JSON file
        #[arg(long)]
        client_secret: String,

        /// SDM project ID (from Device Access console)
        #[arg(long)]
        project_id: String,
    },
}

#[derive(Subcommand)]
enum DeviceAction {
    /// List all thermostats
    List,
    /// Show current status of a thermostat
    Status {
        /// Device ID (or full device name)
        id: String,
    },
}

#[derive(Subcommand)]
enum SetAction {
    /// Set target temperature (in Fahrenheit)
    Temp {
        /// Device ID (or full device name)
        id: String,
        /// Target temperature in Fahrenheit
        temp_f: f64,
    },
    /// Set thermostat mode (heat, cool, heatcool, or off)
    Mode {
        /// Device ID (or full device name)
        id: String,
        /// Mode: heat, cool, heatcool, or off
        mode: String,
    },
}

#[tokio::main]
async fn main() {
    let cli = Cli::parse();

    let result = match cli.command {
        Commands::Auth { action } => match action {
            AuthAction::Login {
                client_secret,
                project_id,
            } => commands::auth_login(&client_secret, &project_id).await,
        },
        Commands::Devices { action } => match action {
            DeviceAction::List => commands::list_devices().await,
            DeviceAction::Status { id } => commands::device_status(&id).await,
        },
        Commands::Set { action } => match action {
            SetAction::Temp { id, temp_f } => commands::set_temperature(&id, temp_f).await,
            SetAction::Mode { id, mode } => commands::set_mode(&id, &mode).await,
        },
    };

    if let Err(e) = result {
        eprintln!("Error: {e}");
        std::process::exit(1);
    }
}
