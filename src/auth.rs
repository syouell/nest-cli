use std::path::PathBuf;
use yup_oauth2::authenticator::Authenticator;
use yup_oauth2::{InstalledFlowAuthenticator, InstalledFlowReturnMethod};

type BoxError = Box<dyn std::error::Error>;
type HttpsConnector = hyper_rustls::HttpsConnector<hyper_util::client::legacy::connect::HttpConnector>;

fn config_dir() -> Result<PathBuf, BoxError> {
    let dir = dirs::config_dir()
        .ok_or("Could not determine config directory")?
        .join("nest-cli");
    std::fs::create_dir_all(&dir)?;
    Ok(dir)
}

fn token_path() -> Result<PathBuf, BoxError> {
    Ok(config_dir()?.join("tokens.json"))
}

fn client_secret_path() -> Result<PathBuf, BoxError> {
    Ok(config_dir()?.join("client_secret.json"))
}

fn project_id_path() -> Result<PathBuf, BoxError> {
    Ok(config_dir()?.join("project_id"))
}

/// Run the OAuth2 installed-app login flow and persist tokens.
pub async fn login(client_secret_file: &str, project_id: &str) -> Result<(), BoxError> {
    let secret = yup_oauth2::read_application_secret(client_secret_file).await?;

    // Copy the client secret to config dir for later use
    std::fs::copy(client_secret_file, client_secret_path()?)?;

    // Save the project ID
    std::fs::write(project_id_path()?, project_id)?;

    let auth = InstalledFlowAuthenticator::builder(secret, InstalledFlowReturnMethod::HTTPRedirect)
        .persist_tokens_to_disk(token_path()?)
        .build()
        .await?;

    // Request the SDM scope to trigger the browser-based OAuth flow
    let scopes = &["https://www.googleapis.com/auth/sdm.service"];
    auth.token(scopes).await?;

    println!("Login successful! Tokens saved.");
    Ok(())
}

/// Build an authenticator from previously saved credentials.
pub async fn get_authenticator() -> Result<Authenticator<HttpsConnector>, BoxError> {
    let secret_path = client_secret_path()?;
    if !secret_path.exists() {
        return Err("Not logged in. Run `nest-cli auth login` first.".into());
    }

    let secret = yup_oauth2::read_application_secret(&secret_path).await?;

    let auth = InstalledFlowAuthenticator::builder(secret, InstalledFlowReturnMethod::HTTPRedirect)
        .persist_tokens_to_disk(token_path()?)
        .build()
        .await?;

    Ok(auth)
}

/// Read the saved SDM project ID.
pub fn get_project_id() -> Result<String, BoxError> {
    let path = project_id_path()?;
    if !path.exists() {
        return Err("No project ID saved. Run `nest-cli auth login` first.".into());
    }
    Ok(std::fs::read_to_string(path)?.trim().to_string())
}
