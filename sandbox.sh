# 镜像发布在 GHCR（ghcr.io/akiyo-dayo/）。
cd sandbox

if [ "$1" == "--pull" ]; then
    sudo docker pull ghcr.io/akiyo-dayo/nekro-agent-byakiyo-sandbox
fi

if [ "$1" == "--build" ]; then
    sudo docker build -t nekro-agent-sandbox:latest .
fi

if [ "$1" == "--push" ]; then
    sudo docker tag nekro-agent-sandbox:latest ghcr.io/akiyo-dayo/nekro-agent-byakiyo-sandbox:latest
    sudo docker push ghcr.io/akiyo-dayo/nekro-agent-byakiyo-sandbox:latest
fi
