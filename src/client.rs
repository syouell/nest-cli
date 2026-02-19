use google_smartdevicemanagement1::api::{
    GoogleHomeEnterpriseSdmV1Device, GoogleHomeEnterpriseSdmV1ExecuteDeviceCommandRequest,
};
use google_smartdevicemanagement1::SmartDeviceManagement;
use serde_json::Value;
use std::collections::HashMap;

type BoxError = Box<dyn std::error::Error>;
type Connector = hyper_rustls::HttpsConnector<hyper_util::client::legacy::connect::HttpConnector>;
type Hub = SmartDeviceManagement<Connector>;

pub struct Client {
    hub: Hub,
    project_id: String,
}

impl Client {
    pub async fn new() -> Result<Self, BoxError> {
        let auth = crate::auth::get_authenticator().await?;
        let project_id = crate::auth::get_project_id()?;

        let connector = hyper_rustls::HttpsConnectorBuilder::new()
            .with_native_roots()?
            .https_only()
            .enable_http2()
            .build();

        let http_client = hyper_util::client::legacy::Client::builder(
            hyper_util::rt::TokioExecutor::new(),
        )
        .build(connector);

        let hub = SmartDeviceManagement::new(http_client, auth);

        Ok(Self { hub, project_id })
    }

    fn parent(&self) -> String {
        format!("enterprises/{}", self.project_id)
    }

    /// Resolve a device ID: if it already contains "enterprises/", use as-is,
    /// otherwise prepend the project path.
    fn resolve_device_name(&self, id: &str) -> String {
        if id.starts_with("enterprises/") {
            id.to_string()
        } else {
            format!("{}/devices/{}", self.parent(), id)
        }
    }

    pub async fn list_devices(&self) -> Result<Vec<GoogleHomeEnterpriseSdmV1Device>, BoxError> {
        let (_, response) = self.hub.enterprises().devices_list(&self.parent()).doit().await?;
        Ok(response.devices.unwrap_or_default())
    }

    pub async fn get_device(&self, id: &str) -> Result<GoogleHomeEnterpriseSdmV1Device, BoxError> {
        let name = self.resolve_device_name(id);
        let (_, device) = self.hub.enterprises().devices_get(&name).doit().await?;
        Ok(device)
    }

    pub async fn execute_command(
        &self,
        id: &str,
        command: &str,
        params: HashMap<String, Value>,
    ) -> Result<(), BoxError> {
        let name = self.resolve_device_name(id);
        let request = GoogleHomeEnterpriseSdmV1ExecuteDeviceCommandRequest {
            command: Some(command.to_string()),
            params: Some(params),
        };
        self.hub
            .enterprises()
            .devices_execute_command(request, &name)
            .doit()
            .await?;
        Ok(())
    }
}
